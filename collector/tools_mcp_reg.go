// tools_mcp_reg.go — Registers the existing collector tools as MCP tools.
//
// This bridges the existing CLI commands (get-scale-metrics, get-metric,
// get-replicas, scale, pod-status) into the MCP tool registry so they
// are available when running as `collector mcp-server`.
package main

import (
	"context"
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func init() {

	// ── get_scale_metrics ─────────────────────────────────────────────────────
	registerTool(
		mcpToolDef{
			Name: "get_scale_metrics",
			Description: "Get all scaling-relevant metrics in one shot: active_connections, " +
				"in_flight_requests, p99_latency_ms, rps_total, error_rate_5xx. " +
				"Always call this first before making a scaling decision.",
			InputSchema: map[string]interface{}{
				"type":       "object",
				"properties": map[string]interface{}{},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			queries := map[string]string{
				"active_connections": `sum(active_connections)`,
				"in_flight_requests": `sum(http_requests_in_flight)`,
				"p99_latency_ms":     `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[2m])) by (le)) * 1000`,
				"rps_total":          `sum(rate(http_requests_total[1m]))`,
				"error_rate_5xx":     `sum(rate(http_requests_total{status=~"5.."}[1m])) / sum(rate(http_requests_total[1m]))`,
			}
			results := map[string]interface{}{}
			errors := map[string]string{}
			for name, q := range queries {
				val, err := queryPrometheus(q)
				if err != nil {
					errors[name] = err.Error()
				} else {
					results[name] = val
				}
			}
			results["timestamp"] = time.Now().UTC().Format(time.RFC3339)
			if len(errors) > 0 {
				results["query_errors"] = errors
			}
			return results, nil
		},
	)

	// ── get_metric ────────────────────────────────────────────────────────────
	registerTool(
		mcpToolDef{
			Name:        "get_metric",
			Description: "Execute a custom PromQL query and return its current value.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"query": map[string]interface{}{
						"type":        "string",
						"description": "PromQL query to execute",
					},
				},
				"required": []string{"query"},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			q, _ := args["query"].(string)
			if q == "" {
				return nil, fmt.Errorf("query is required")
			}
			val, err := queryPrometheus(q)
			if err != nil {
				return nil, err
			}
			return MetricResult{
				Query:     q,
				Value:     val,
				Timestamp: time.Now().UTC(),
			}, nil
		},
	)

	// ── get_replicas ──────────────────────────────────────────────────────────
	registerTool(
		mcpToolDef{
			Name:        "get_replicas",
			Description: "Get current replica count and readiness status for a deployment.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"deployment": map[string]interface{}{"type": "string", "description": "Deployment name"},
					"namespace":  map[string]interface{}{"type": "string", "description": "Kubernetes namespace"},
				},
				"required": []string{"deployment", "namespace"},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			dep, _ := args["deployment"].(string)
			ns, _ := args["namespace"].(string)
			if ns == "" {
				ns = "turtlecrawl"
			}
			cs, err := k8sClient()
			if err != nil {
				return nil, err
			}
			d, err := cs.AppsV1().Deployments(ns).Get(context.Background(), dep, metav1.GetOptions{})
			if err != nil {
				return nil, fmt.Errorf("getting deployment: %w", err)
			}
			return ReplicasResult{
				Deployment: dep,
				Namespace:  ns,
				Replicas:   *d.Spec.Replicas,
				Ready:      d.Status.ReadyReplicas,
				Available:  d.Status.AvailableReplicas,
			}, nil
		},
	)

	// ── scale_deployment ──────────────────────────────────────────────────────
	registerTool(
		mcpToolDef{
			Name: "scale_deployment",
			Description: "Scale a deployment to a given number of replicas. " +
				"IMPORTANT: Always call check_scale_gate before scaling down. " +
				"Call set_keda_paused(true) before manual scaling to prevent " +
				"KEDA from immediately overriding your change, then resume KEDA after.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"deployment": map[string]interface{}{"type": "string"},
					"namespace":  map[string]interface{}{"type": "string"},
					"replicas": map[string]interface{}{
						"type":        "integer",
						"description": "Target replica count (1–50)",
					},
					"reason": map[string]interface{}{
						"type":        "string",
						"description": "Brief explanation of why this scale action is needed",
					},
				},
				"required": []string{"deployment", "namespace", "replicas", "reason"},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			dep, _ := args["deployment"].(string)
			ns, _ := args["namespace"].(string)
			if ns == "" {
				ns = "turtlecrawl"
			}
			replicasF, ok := args["replicas"].(float64)
			if !ok {
				return nil, fmt.Errorf("replicas must be a number")
			}
			replicas := int32(replicasF)
			if replicas < 1 || replicas > 50 {
				return nil, fmt.Errorf("replicas must be between 1 and 50, got %d", replicas)
			}

			cs, err := k8sClient()
			if err != nil {
				return nil, err
			}
			d, err := cs.AppsV1().Deployments(ns).Get(context.Background(), dep, metav1.GetOptions{})
			if err != nil {
				return nil, fmt.Errorf("getting deployment: %w", err)
			}
			oldReplicas := *d.Spec.Replicas

			scale, err := cs.AppsV1().Deployments(ns).GetScale(context.Background(), dep, metav1.GetOptions{})
			if err != nil {
				return nil, fmt.Errorf("getting scale: %w", err)
			}
			scale.Spec.Replicas = replicas
			_, err = cs.AppsV1().Deployments(ns).UpdateScale(context.Background(), dep, scale, metav1.UpdateOptions{})
			if err != nil {
				return nil, fmt.Errorf("updating scale: %w", err)
			}

			return ScaleResult{
				Deployment:  dep,
				Namespace:   ns,
				OldReplicas: oldReplicas,
				NewReplicas: replicas,
				Status:      "scaled",
			}, nil
		},
	)

	// ── get_pod_status ────────────────────────────────────────────────────────
	registerTool(
		mcpToolDef{
			Name:        "get_pod_status",
			Description: "Get per-pod readiness status, restart count, and node assignment.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"deployment": map[string]interface{}{"type": "string"},
					"namespace":  map[string]interface{}{"type": "string"},
				},
				"required": []string{"deployment", "namespace"},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			dep, _ := args["deployment"].(string)
			ns, _ := args["namespace"].(string)
			if ns == "" {
				ns = "turtlecrawl"
			}
			return getPodStatus(dep, ns)
		},
	)
}
