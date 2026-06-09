// mcp_server.go — MCP JSON-RPC 2.0 server over stdio.
//
// Protocol: Model Context Protocol (MCP) 2024-11-05
// Transport: newline-delimited JSON over stdin/stdout
//
// The Python agent spawns this process with `collector mcp-server`
// and communicates via stdin/stdout JSON-RPC.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

// ─── JSON-RPC 2.0 wire types ─────────────────────────────────────────────────

type mcpRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type mcpResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Result  interface{}     `json:"result,omitempty"`
	Error   *mcpRPCError    `json:"error,omitempty"`
}

type mcpRPCError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// ─── MCP protocol types ───────────────────────────────────────────────────────

type mcpToolDef struct {
	Name        string                 `json:"name"`
	Description string                 `json:"description"`
	InputSchema map[string]interface{} `json:"inputSchema"`
}

type mcpContent struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

type mcpCallResult struct {
	Content []mcpContent `json:"content"`
	IsError bool         `json:"isError,omitempty"`
}

// ─── Tool handler registry ────────────────────────────────────────────────────

// toolHandler is a function that receives JSON arguments and returns a result.
type toolHandler func(args map[string]interface{}) (interface{}, error)

var toolRegistry = map[string]toolHandler{}

// registerTool adds a tool to the registry. Called from each *_mcp.go file's init().
func registerTool(def mcpToolDef, fn toolHandler) {
	toolRegistry[def.Name] = fn
	allToolDefs = append(allToolDefs, def)
}

var allToolDefs []mcpToolDef

// ─── Server loop ──────────────────────────────────────────────────────────────

func runMCPServer() {
	scanner := bufio.NewScanner(os.Stdin)
	// Increase buffer size for large JSON payloads
	buf := make([]byte, 0, 1<<20) // 1MB
	scanner.Buffer(buf, 1<<20)

	writer := bufio.NewWriter(os.Stdout)

	sendResponse := func(resp mcpResponse) {
		out, _ := json.Marshal(resp)
		writer.Write(out)
		writer.WriteByte('\n')
		writer.Flush()
	}

	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}

		var req mcpRequest
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			// Invalid JSON — send parse error but only if we have an id
			sendResponse(mcpResponse{
				JSONRPC: "2.0",
				Error:   &mcpRPCError{Code: -32700, Message: "Parse error"},
			})
			continue
		}

		// Notifications (no id) — handle and move on, no response needed
		if req.ID == nil {
			switch req.Method {
			case "notifications/initialized":
				// Client confirmed initialization — nothing to do
			}
			continue
		}

		switch req.Method {

		case "initialize":
			sendResponse(mcpResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Result: map[string]interface{}{
					"protocolVersion": "2024-11-05",
					"capabilities": map[string]interface{}{
						"tools": map[string]interface{}{},
					},
					"serverInfo": map[string]interface{}{
						"name":    "turtlecrawl-collector",
						"version": "2.0.0",
					},
				},
			})

		case "tools/list":
			sendResponse(mcpResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Result: map[string]interface{}{
					"tools": allToolDefs,
				},
			})

		case "tools/call":
			var params struct {
				Name      string                 `json:"name"`
				Arguments map[string]interface{} `json:"arguments"`
			}
			if err := json.Unmarshal(req.Params, &params); err != nil {
				sendResponse(mcpResponse{
					JSONRPC: "2.0",
					ID:      req.ID,
					Error:   &mcpRPCError{Code: -32602, Message: "Invalid params: " + err.Error()},
				})
				continue
			}

			handler, ok := toolRegistry[params.Name]
			if !ok {
				sendResponse(mcpResponse{
					JSONRPC: "2.0",
					ID:      req.ID,
					Error:   &mcpRPCError{Code: -32601, Message: fmt.Sprintf("Unknown tool: %s", params.Name)},
				})
				continue
			}

			result, err := handler(params.Arguments)
			var callResult mcpCallResult
			if err != nil {
				errJSON, _ := json.Marshal(map[string]string{"error": err.Error()})
				callResult = mcpCallResult{
					Content: []mcpContent{{Type: "text", Text: string(errJSON)}},
					IsError: true,
				}
			} else {
				text, _ := json.Marshal(result)
				callResult = mcpCallResult{
					Content: []mcpContent{{Type: "text", Text: string(text)}},
				}
			}

			sendResponse(mcpResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Result:  callResult,
			})

		default:
			sendResponse(mcpResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Error:   &mcpRPCError{Code: -32601, Message: fmt.Sprintf("Method not found: %s", req.Method)},
			})
		}
	}

	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "mcp-server: scanner error: %v\n", err)
	}
}

// ─── cobra subcommand ─────────────────────────────────────────────────────────

var mcpServerCmd = &cobra.Command{
	Use:   "mcp-server",
	Short: "Run as an MCP server (JSON-RPC 2.0 over stdio)",
	Long: `Starts the collector as an MCP server.

The Python agent spawns this process and communicates via stdin/stdout
using the Model Context Protocol (MCP) JSON-RPC 2.0 protocol.

All 11 Go-side tools are exposed as MCP tools. The Python agent adds
run_load_test and wait as native tools, giving Gemini 13 tools total.`,
	Run: func(cmd *cobra.Command, args []string) {
		runMCPServer()
	},
}

func init() {
	rootCmd.AddCommand(mcpServerCmd)
}
