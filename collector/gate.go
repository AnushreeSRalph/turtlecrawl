// gate.go — Scale-down safety gate (MCP tool: check_scale_gate).
//
// Runs 6 conditions in parallel and reports pass/fail for each.
// The agent should only scale down when all_passed is true.
package main

import (
	"context"
	"fmt"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// GateCondition holds the result of a single gate check.
type GateCondition struct {
	Name    string `json:"name"`
	Pass    bool   `json:"pass"`
	Value   string `json:"value"`   // actual measured value
	Limit   string `json:"limit"`   // threshold that must be met
	Message string `json:"message"` // human-readable explanation
}

// GateResult is the full gate check output.
type GateResult struct {
	AllPassed  bool            `json:"all_passed"`
	Conditions []GateCondition `json:"conditions"`
	Deployment string          `json:"deployment"`
	Namespace  string          `json:"namespace"`
	Timestamp  string          `json:"timestamp"`
}

// checkScaleGate runs all 6 scale-down safety conditions in parallel.
func checkScaleGate(deployment, namespace string) (GateResult, error) {
	result := GateResult{
		Deployment: deployment,
		Namespace:  namespace,
		Timestamp:  time.Now().UTC().Format(time.RFC3339),
	}

	type condResult struct {
		cond GateCondition
		idx  int
	}

	conditions := make([]GateCondition, 6)
	var wg sync.WaitGroup
	ch := make(chan condResult, 6)

	// ── Condition 0: max active_connections per pod < 2 ─────────────────────
	// We use max() not sum(): each pod holds ~1 persistent connection from the
	// Prometheus scraper, so sum(active_connections) always equals pod count.
	// max(active_connections) < 2 means no single pod sees more than the
	// background scrape connection — i.e., no real user traffic is flowing.
	wg.Add(1)
	go func() {
		defer wg.Done()
		val, err := queryPrometheus(`max(active_connections)`)
		cond := GateCondition{Name: "active_connections_zero", Limit: "< 2 per pod"}
		if err != nil {
			cond.Pass = false
			cond.Value = "query_error"
			cond.Message = fmt.Sprintf("Prometheus query failed: %v", err)
		} else {
			cond.Value = fmt.Sprintf("%.0f max/pod", val)
			cond.Pass = val < 2
			if cond.Pass {
				cond.Message = fmt.Sprintf("max %.0f connection/pod — only scrape traffic, no user load", val)
			} else {
				cond.Message = fmt.Sprintf("%.0f active connections on busiest pod — user traffic still flowing", val)
			}
		}
		ch <- condResult{cond, 0}
	}()

	// ── Condition 1: max in_flight_requests per pod < 2 ─────────────────────
	// Same reasoning as active_connections: sum() inflates with pod count.
	// max(http_requests_in_flight) < 2 means no individual pod is actively
	// handling more than a single background probe request.
	wg.Add(1)
	go func() {
		defer wg.Done()
		val, err := queryPrometheus(`max(http_requests_in_flight)`)
		cond := GateCondition{Name: "in_flight_requests_low", Limit: "< 2 per pod"}
		if err != nil {
			cond.Pass = false
			cond.Value = "query_error"
			cond.Message = fmt.Sprintf("Prometheus query failed: %v", err)
		} else {
			cond.Value = fmt.Sprintf("%.0f max/pod", val)
			cond.Pass = val < 2
			if cond.Pass {
				cond.Message = fmt.Sprintf("max %.0f in-flight/pod — within safe limit", val)
			} else {
				cond.Message = fmt.Sprintf("%.0f in-flight requests on busiest pod (limit: <2/pod)", val)
			}
		}
		ch <- condResult{cond, 1}
	}()

	// ── Condition 2: p99 latency < 120ms ─────────────────────────────────────
	wg.Add(1)
	go func() {
		defer wg.Done()
		val, err := queryPrometheus(
			`histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[2m])) by (le)) * 1000`,
		)
		cond := GateCondition{Name: "p99_latency_ok", Limit: "< 120ms"}
		if err != nil {
			cond.Pass = false
			cond.Value = "query_error"
			cond.Message = fmt.Sprintf("Prometheus query failed: %v", err)
		} else {
			cond.Value = fmt.Sprintf("%.1fms", val)
			cond.Pass = val < 120
			if cond.Pass {
				cond.Message = fmt.Sprintf("p99 latency %.1fms — SLO met", val)
			} else {
				cond.Message = fmt.Sprintf("p99 latency %.1fms exceeds 120ms SLO", val)
			}
		}
		ch <- condResult{cond, 2}
	}()

	// ── Condition 3: error rate < 1% ─────────────────────────────────────────
	wg.Add(1)
	go func() {
		defer wg.Done()
		val, err := queryPrometheus(
			`sum(rate(http_requests_total{status=~"5.."}[1m])) / sum(rate(http_requests_total[1m]))`,
		)
		cond := GateCondition{Name: "error_rate_ok", Limit: "< 1%"}
		if err != nil {
			cond.Pass = false
			cond.Value = "query_error"
			cond.Message = fmt.Sprintf("Prometheus query failed: %v", err)
		} else {
			pct := val * 100
			cond.Value = fmt.Sprintf("%.2f%%", pct)
			cond.Pass = pct < 1.0
			if cond.Pass {
				cond.Message = fmt.Sprintf("Error rate %.2f%% — within SLO", pct)
			} else {
				cond.Message = fmt.Sprintf("Error rate %.2f%% exceeds 1%% SLO", pct)
			}
		}
		ch <- condResult{cond, 3}
	}()

	// ── Condition 4: per-pod RPS headroom < 80 RPS/pod ───────────────────────
	wg.Add(1)
	go func() {
		defer wg.Done()
		cond := GateCondition{Name: "per_pod_rps_headroom", Limit: "< 80 RPS/pod"}

		cs, err := k8sClient()
		if err != nil {
			cond.Pass = false
			cond.Value = "k8s_error"
			cond.Message = fmt.Sprintf("k8s client error: %v", err)
			ch <- condResult{cond, 4}
			return
		}
		dep, err := cs.AppsV1().Deployments(namespace).Get(
			context.Background(), deployment, metav1.GetOptions{},
		)
		if err != nil {
			cond.Pass = false
			cond.Value = "k8s_error"
			cond.Message = fmt.Sprintf("Deployment get error: %v", err)
			ch <- condResult{cond, 4}
			return
		}
		replicas := float64(*dep.Spec.Replicas)
		if replicas < 1 {
			replicas = 1
		}
		rps, err := queryPrometheus(`sum(rate(http_requests_total[1m]))`)
		if err != nil {
			cond.Pass = false
			cond.Value = "query_error"
			cond.Message = fmt.Sprintf("Prometheus query failed: %v", err)
			ch <- condResult{cond, 4}
			return
		}
		perPodRPS := rps / replicas
		cond.Value = fmt.Sprintf("%.1f RPS/pod", perPodRPS)
		cond.Pass = perPodRPS < 80
		if cond.Pass {
			cond.Message = fmt.Sprintf("%.1f RPS/pod across %d pods — headroom available", perPodRPS, int(replicas))
		} else {
			cond.Message = fmt.Sprintf("%.1f RPS/pod — insufficient headroom to scale down", perPodRPS)
		}
		ch <- condResult{cond, 4}
	}()

	// ── Condition 5: all pods Ready ───────────────────────────────────────────
	wg.Add(1)
	go func() {
		defer wg.Done()
		cond := GateCondition{Name: "all_pods_ready", Limit: "100% ready"}

		cs, err := k8sClient()
		if err != nil {
			cond.Pass = false
			cond.Value = "k8s_error"
			cond.Message = fmt.Sprintf("k8s client error: %v", err)
			ch <- condResult{cond, 5}
			return
		}
		dep, err := cs.AppsV1().Deployments(namespace).Get(
			context.Background(), deployment, metav1.GetOptions{},
		)
		if err != nil {
			cond.Pass = false
			cond.Value = "k8s_error"
			cond.Message = fmt.Sprintf("Deployment get error: %v", err)
			ch <- condResult{cond, 5}
			return
		}
		desired := *dep.Spec.Replicas
		ready := dep.Status.ReadyReplicas
		cond.Value = fmt.Sprintf("%d/%d ready", ready, desired)
		cond.Pass = ready == desired && desired > 0
		if cond.Pass {
			cond.Message = fmt.Sprintf("All %d/%d pods Ready", ready, desired)
		} else {
			cond.Message = fmt.Sprintf("%d/%d pods Ready — wait for rollout to complete", ready, desired)
		}
		ch <- condResult{cond, 5}
	}()

	wg.Wait()
	close(ch)
	for cr := range ch {
		conditions[cr.idx] = cr.cond
	}

	result.Conditions = conditions
	result.AllPassed = true
	for _, c := range conditions {
		if !c.Pass {
			result.AllPassed = false
			break
		}
	}

	return result, nil
}

// ─── MCP tool registration ────────────────────────────────────────────────────

func init() {
	registerTool(
		mcpToolDef{
			Name: "check_scale_gate",
			Description: "Run all 6 scale-down safety conditions in parallel: " +
				"max(active_connections)<2/pod, max(in_flight)<2/pod, p99<120ms, error_rate<1%, " +
				"per_pod_rps_headroom<80, all_pods_ready. " +
				"Returns all_passed:true only when all 6 pass. " +
				"Always call this before scaling down a deployment.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"deployment": map[string]interface{}{
						"type":        "string",
						"description": "Deployment name to check",
					},
					"namespace": map[string]interface{}{
						"type":        "string",
						"description": "Kubernetes namespace",
					},
				},
				"required": []string{"deployment", "namespace"},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			dep, _ := args["deployment"].(string)
			ns, ok := args["namespace"].(string)
			if !ok || ns == "" {
				ns = "turtlecrawl"
			}
			return checkScaleGate(dep, ns)
		},
	)
}
