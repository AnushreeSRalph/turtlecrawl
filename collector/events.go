// events.go — Kubernetes events tool for the MCP server.
//
// Tool: list_k8s_events
// Returns recent K8s Warning events in a namespace, optionally filtered
// to events involving a specific deployment.
package main

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// K8sEvent is a single event returned to the agent.
type K8sEvent struct {
	Type      string `json:"type"`       // Normal or Warning
	Reason    string `json:"reason"`
	Object    string `json:"object"`     // Kind/Name
	Message   string `json:"message"`
	Count     int32  `json:"count"`
	FirstSeen string `json:"first_seen"`
	LastSeen  string `json:"last_seen"`
}

// K8sEventsResult is returned by list_k8s_events.
type K8sEventsResult struct {
	Namespace  string     `json:"namespace"`
	Deployment string     `json:"deployment,omitempty"`
	Events     []K8sEvent `json:"events"`
	Total      int        `json:"total"`
	Timestamp  string     `json:"timestamp"`
}

func listK8sEvents(namespace, deployment string, warningOnly bool, limit int) (K8sEventsResult, error) {
	cs, err := k8sClient()
	if err != nil {
		return K8sEventsResult{}, err
	}

	fieldSelector := ""
	if warningOnly {
		fieldSelector = "type=Warning"
	}

	eventList, err := cs.CoreV1().Events(namespace).List(context.Background(), metav1.ListOptions{
		FieldSelector: fieldSelector,
	})
	if err != nil {
		return K8sEventsResult{}, fmt.Errorf("listing events: %w", err)
	}

	// Sort by last seen (most recent first)
	items := eventList.Items
	sort.Slice(items, func(i, j int) bool {
		return items[i].LastTimestamp.After(items[j].LastTimestamp.Time)
	})

	var events []K8sEvent
	for _, e := range items {
		if limit > 0 && len(events) >= limit {
			break
		}

		// Filter to deployment if specified
		if deployment != "" {
			involved := e.InvolvedObject
			// Include events for the Deployment itself, ReplicaSets it owns, and its Pods
			isRelevant := false
			if strings.EqualFold(involved.Kind, "Deployment") && involved.Name == deployment {
				isRelevant = true
			} else if strings.EqualFold(involved.Kind, "ReplicaSet") && strings.HasPrefix(involved.Name, deployment+"-") {
				isRelevant = true
			} else if strings.EqualFold(involved.Kind, "Pod") && strings.HasPrefix(involved.Name, deployment+"-") {
				isRelevant = true
			}
			if !isRelevant {
				continue
			}
		}

		ev := K8sEvent{
			Type:    e.Type,
			Reason:  e.Reason,
			Object:  fmt.Sprintf("%s/%s", e.InvolvedObject.Kind, e.InvolvedObject.Name),
			Message: e.Message,
			Count:   e.Count,
		}

		if !e.FirstTimestamp.IsZero() {
			ev.FirstSeen = e.FirstTimestamp.UTC().Format(time.RFC3339)
		}
		if !e.LastTimestamp.IsZero() {
			ev.LastSeen = e.LastTimestamp.UTC().Format(time.RFC3339)
		}

		// Filter out non-Warning Normal events when warningOnly is false
		// but still show them to the agent for context
		if e.Type == corev1.EventTypeWarning || !warningOnly {
			events = append(events, ev)
		}
	}

	return K8sEventsResult{
		Namespace:  namespace,
		Deployment: deployment,
		Events:     events,
		Total:      len(events),
		Timestamp:  time.Now().UTC().Format(time.RFC3339),
	}, nil
}

// ─── MCP tool registration ────────────────────────────────────────────────────

func init() {
	registerTool(
		mcpToolDef{
			Name: "list_k8s_events",
			Description: "List recent Kubernetes events for a namespace, " +
				"optionally filtered to a specific deployment. " +
				"Warning events indicate OOMKilled pods, failed pulls, " +
				"BackOff restarts, or scheduling failures. " +
				"Call this when pod status looks unhealthy or after an unexpected scale event.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"namespace": map[string]interface{}{
						"type":        "string",
						"description": "Kubernetes namespace",
					},
					"deployment": map[string]interface{}{
						"type":        "string",
						"description": "Filter events to this deployment (optional)",
					},
					"warning_only": map[string]interface{}{
						"type":        "boolean",
						"description": "If true, return only Warning events (default: true)",
					},
					"limit": map[string]interface{}{
						"type":        "integer",
						"description": "Max number of events to return (default: 20)",
					},
				},
				"required": []string{"namespace"},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			ns, _ := args["namespace"].(string)
			if ns == "" {
				ns = "turtlecrawl"
			}
			dep, _ := args["deployment"].(string)

			warningOnly := true // default
			if v, ok := args["warning_only"].(bool); ok {
				warningOnly = v
			}

			limit := 20 // default
			if v, ok := args["limit"].(float64); ok {
				limit = int(v)
			}

			return listK8sEvents(ns, dep, warningOnly, limit)
		},
	)
}
