package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"time"

	"github.com/spf13/cobra"
)

// MetricResult is the JSON output of the get-metric command.
type MetricResult struct {
	Query     string    `json:"query"`
	Value     float64   `json:"value"`
	Timestamp time.Time `json:"timestamp"`
	Labels    []string  `json:"labels,omitempty"`
}

// MetricError wraps query errors.
type MetricError struct {
	Error string `json:"error"`
	Query string `json:"query"`
}

// prometheusURL is the Prometheus HTTP API base, configurable via env.
func prometheusURL() string {
	if u := os.Getenv("PROMETHEUS_URL"); u != "" {
		return u
	}
	return "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
}

func queryPrometheus(query string) (float64, error) {
	base := prometheusURL()
	apiURL := fmt.Sprintf("%s/api/v1/query", base)

	params := url.Values{}
	params.Set("query", query)

	resp, err := http.Get(apiURL + "?" + params.Encode())
	if err != nil {
		return 0, fmt.Errorf("prometheus request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, fmt.Errorf("reading response: %w", err)
	}

	var result struct {
		Status string `json:"status"`
		Data   struct {
			ResultType string `json:"resultType"`
			Result     []struct {
				Metric map[string]string `json:"metric"`
				Value  [2]interface{}    `json:"value"`
			} `json:"result"`
		} `json:"data"`
	}

	if err := json.Unmarshal(body, &result); err != nil {
		return 0, fmt.Errorf("parsing prometheus response: %w", err)
	}
	if result.Status != "success" {
		return 0, fmt.Errorf("prometheus query failed: status=%s", result.Status)
	}
	if len(result.Data.Result) == 0 {
		return 0, nil // no data = 0
	}

	// Take the first result value (string → float64)
	rawVal, ok := result.Data.Result[0].Value[1].(string)
	if !ok {
		return 0, fmt.Errorf("unexpected value type from prometheus")
	}
	val, err := strconv.ParseFloat(rawVal, 64)
	if err != nil {
		return 0, fmt.Errorf("parsing value %q: %w", rawVal, err)
	}
	return val, nil
}

// ─── get-metric command ───────────────────────────────────────────────────────

var (
	metricQuery  string
	metricWindow string
)

var getMetricCmd = &cobra.Command{
	Use:   "get-metric",
	Short: "Query a Prometheus metric and return its current value as JSON",
	Example: `  collector get-metric --query 'sum(rate(http_requests_total[1m]))'
  collector get-metric --query 'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[2m]))'`,
	Run: func(cmd *cobra.Command, args []string) {
		query := metricQuery
		// If window placeholder is used, substitute it
		if metricWindow != "" {
			// Allow queries with {window} placeholder
		}

		val, err := queryPrometheus(query)
		if err != nil {
			out, _ := json.Marshal(MetricError{Error: err.Error(), Query: query})
			fmt.Println(string(out))
			os.Exit(1)
		}

		out, _ := json.Marshal(MetricResult{
			Query:     query,
			Value:     val,
			Timestamp: time.Now().UTC(),
		})
		fmt.Println(string(out))
	},
}

// ─── get-scale-metrics command ───────────────────────────────────────────────
// Returns a bundle of all metrics the agent needs in one shot.

type ScaleMetrics struct {
	ActiveConnections   float64 `json:"active_connections"`
	InFlightRequests    float64 `json:"in_flight_requests"`
	P99LatencyMs        float64 `json:"p99_latency_ms"`
	RPSTotal            float64 `json:"rps_total"`
	ErrorRate           float64 `json:"error_rate_5xx"`
	Timestamp           string  `json:"timestamp"`
}

var getScaleMetricsCmd = &cobra.Command{
	Use:   "get-scale-metrics",
	Short: "Return all scaling-relevant metrics in one JSON bundle",
	Run: func(cmd *cobra.Command, args []string) {
		queries := map[string]string{
			"active_connections": `sum(active_connections)`,
			"in_flight_requests": `sum(http_requests_in_flight)`,
			"p99_latency_ms":     `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[2m])) by (le)) * 1000`,
			"rps_total":          `sum(rate(http_requests_total[1m]))`,
			"error_rate_5xx":     `sum(rate(http_requests_total{status=~"5.."}[1m])) / sum(rate(http_requests_total[1m]))`,
		}

		results := map[string]float64{}
		errors := map[string]string{}

		for name, q := range queries {
			val, err := queryPrometheus(q)
			if err != nil {
				errors[name] = err.Error()
			} else {
				results[name] = val
			}
		}

		out := map[string]interface{}{
			"active_connections": results["active_connections"],
			"in_flight_requests": results["in_flight_requests"],
			"p99_latency_ms":     results["p99_latency_ms"],
			"rps_total":          results["rps_total"],
			"error_rate_5xx":     results["error_rate_5xx"],
			"timestamp":          time.Now().UTC().Format(time.RFC3339),
		}
		if len(errors) > 0 {
			out["query_errors"] = errors
		}

		j, _ := json.Marshal(out)
		fmt.Println(string(j))
	},
}

func init() {
	getMetricCmd.Flags().StringVarP(&metricQuery, "query", "q", "", "PromQL query to execute (required)")
	getMetricCmd.Flags().StringVar(&metricWindow, "window", "1m", "Time window hint (for documentation; embed in your query)")
	_ = getMetricCmd.MarkFlagRequired("query")

	rootCmd.AddCommand(getMetricCmd)
	rootCmd.AddCommand(getScaleMetricsCmd)
}
