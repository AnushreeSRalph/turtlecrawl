// keda.go — KEDA ScaledObject tools for the MCP server.
//
// Tools:
//   get_keda_scaledobject — inspect KEDA's current state for a ScaledObject
//   set_keda_paused       — pause or resume KEDA's autoscaling
//
// KEDA CRDs live in the keda.sh/v1alpha1 API group.
// We use the Kubernetes dynamic client to read/patch them.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	"os"
)

// scaledObjectGVR is the GroupVersionResource for KEDA ScaledObjects.
var scaledObjectGVR = schema.GroupVersionResource{
	Group:    "keda.sh",
	Version:  "v1alpha1",
	Resource: "scaledobjects",
}

// dynamicClient returns a Kubernetes dynamic client.
func dynamicClient() (dynamic.Interface, error) {
	config, err := rest.InClusterConfig()
	if err != nil {
		kubeconfig := os.Getenv("KUBECONFIG")
		if kubeconfig == "" {
			kubeconfig = os.Getenv("HOME") + "/.kube/config"
		}
		config, err = clientcmd.BuildConfigFromFlags("", kubeconfig)
		if err != nil {
			return nil, fmt.Errorf("building k8s config: %w", err)
		}
	}
	return dynamic.NewForConfig(config)
}

// ─── KEDAStatus ──────────────────────────────────────────────────────────────

// KEDAStatus is returned by get_keda_scaledobject.
type KEDAStatus struct {
	Name             string `json:"name"`
	Namespace        string `json:"namespace"`
	Paused           bool   `json:"paused"`
	CurrentReplicas  int64  `json:"current_replicas"`
	DesiredReplicas  int64  `json:"desired_replicas"`
	LastScaleTime    string `json:"last_scale_time,omitempty"`
	Ready            bool   `json:"ready"`
	ActiveTriggers   []string `json:"active_triggers,omitempty"`
	Timestamp        string `json:"timestamp"`
}

// typedClient returns a typed Kubernetes clientset (needed for HPA reads).
func typedClient() (*kubernetes.Clientset, error) {
	config, err := rest.InClusterConfig()
	if err != nil {
		kubeconfig := os.Getenv("KUBECONFIG")
		if kubeconfig == "" {
			kubeconfig = os.Getenv("HOME") + "/.kube/config"
		}
		config, err = clientcmd.BuildConfigFromFlags("", kubeconfig)
		if err != nil {
			return nil, fmt.Errorf("building k8s config: %w", err)
		}
	}
	return kubernetes.NewForConfig(config)
}

func getKEDAScaledObject(name, namespace string) (KEDAStatus, error) {
	dc, err := dynamicClient()
	if err != nil {
		return KEDAStatus{}, err
	}

	obj, err := dc.Resource(scaledObjectGVR).Namespace(namespace).Get(
		context.Background(), name, metav1.GetOptions{},
	)
	if err != nil {
		return KEDAStatus{}, fmt.Errorf("getting ScaledObject %s/%s: %w", namespace, name, err)
	}

	status := KEDAStatus{
		Name:      name,
		Namespace: namespace,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}

	// Check paused annotation
	annotations := obj.GetAnnotations()
	if annotations != nil {
		if v, ok := annotations["autoscaling.keda.sh/paused"]; ok && v == "true" {
			status.Paused = true
		}
	}

	// Extract status fields from the unstructured object.
	statusField, ok := obj.Object["status"].(map[string]interface{})
	if ok {
		if lst, ok := statusField["lastActiveTime"].(string); ok {
			status.LastScaleTime = lst
		}
		// Check conditions for Ready
		if conds, ok := statusField["conditions"].([]interface{}); ok {
			for _, c := range conds {
				cmap, ok := c.(map[string]interface{})
				if !ok {
					continue
				}
				if cmap["type"] == "Ready" && cmap["status"] == "True" {
					status.Ready = true
				}
			}
		}
		// Active triggers from externalMetricNames
		if metrics, ok := statusField["externalMetricNames"].([]interface{}); ok {
			for _, m := range metrics {
				if s, ok := m.(string); ok {
					status.ActiveTriggers = append(status.ActiveTriggers, s)
				}
			}
		}

		// KEDA v2.14 removed currentReplicaCount/desiredReplicaCount from the
		// ScaledObject status. Read replica counts from the managed HPA instead.
		hpaName, _ := statusField["hpaName"].(string)
		if hpaName != "" {
			if cs, err := typedClient(); err == nil {
				hpa, err := cs.AutoscalingV2().HorizontalPodAutoscalers(namespace).Get(
					context.Background(), hpaName, metav1.GetOptions{},
				)
				if err == nil {
					status.CurrentReplicas = int64(hpa.Status.CurrentReplicas)
					status.DesiredReplicas = int64(hpa.Status.DesiredReplicas)
				}
			}
		}
	}

	return status, nil
}

// ─── SetKEDAPaused ────────────────────────────────────────────────────────────

// KEDAPausedResult is returned by set_keda_paused.
type KEDAPausedResult struct {
	Name      string `json:"name"`
	Namespace string `json:"namespace"`
	Paused    bool   `json:"paused"`
	Message   string `json:"message"`
	Timestamp string `json:"timestamp"`
}

func setKEDAPaused(name, namespace string, paused bool) (KEDAPausedResult, error) {
	dc, err := dynamicClient()
	if err != nil {
		return KEDAPausedResult{}, err
	}

	// Patch the autoscaling.keda.sh/paused annotation
	var pauseValue string
	if paused {
		pauseValue = "true"
	} else {
		pauseValue = "false"
	}

	// Use a strategic merge patch to set/remove the annotation
	patch := map[string]interface{}{
		"metadata": map[string]interface{}{
			"annotations": map[string]interface{}{
				"autoscaling.keda.sh/paused": pauseValue,
			},
		},
	}
	patchBytes, err := json.Marshal(patch)
	if err != nil {
		return KEDAPausedResult{}, fmt.Errorf("marshaling patch: %w", err)
	}

	_, err = dc.Resource(scaledObjectGVR).Namespace(namespace).Patch(
		context.Background(),
		name,
		types.MergePatchType,
		patchBytes,
		metav1.PatchOptions{},
	)
	if err != nil {
		return KEDAPausedResult{}, fmt.Errorf("patching ScaledObject %s/%s: %w", namespace, name, err)
	}

	msg := fmt.Sprintf("KEDA ScaledObject %s/%s autoscaling resumed", namespace, name)
	if paused {
		msg = fmt.Sprintf("KEDA ScaledObject %s/%s autoscaling paused — manual scaling is now safe", namespace, name)
	}

	return KEDAPausedResult{
		Name:      name,
		Namespace: namespace,
		Paused:    paused,
		Message:   msg,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}, nil
}

// ─── MCP tool registration ────────────────────────────────────────────────────

func init() {
	registerTool(
		mcpToolDef{
			Name: "get_keda_scaledobject",
			Description: "Get the current state of a KEDA ScaledObject: " +
				"current_replicas, desired_replicas, last_scale_time, paused status, " +
				"and which triggers are active. " +
				"Use this to understand what KEDA is doing before intervening manually.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"name": map[string]interface{}{
						"type":        "string",
						"description": "ScaledObject name (e.g. sample-app-scaler)",
					},
					"namespace": map[string]interface{}{
						"type":        "string",
						"description": "Kubernetes namespace",
					},
				},
				"required": []string{"name", "namespace"},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			name, _ := args["name"].(string)
			ns, _ := args["namespace"].(string)
			if ns == "" {
				ns = "turtlecrawl"
			}
			return getKEDAScaledObject(name, ns)
		},
	)

	registerTool(
		mcpToolDef{
			Name: "set_keda_paused",
			Description: "Pause or resume KEDA autoscaling for a ScaledObject. " +
				"Call set_keda_paused(paused=true) before calling scale_deployment " +
				"to prevent KEDA from immediately overriding your manual scale. " +
				"Always call set_keda_paused(paused=false) when done to restore " +
				"autonomous KEDA scaling.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"name": map[string]interface{}{
						"type":        "string",
						"description": "ScaledObject name",
					},
					"namespace": map[string]interface{}{
						"type": "string",
					},
					"paused": map[string]interface{}{
						"type":        "boolean",
						"description": "true to pause autoscaling, false to resume",
					},
				},
				"required": []string{"name", "namespace", "paused"},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			name, _ := args["name"].(string)
			ns, _ := args["namespace"].(string)
			if ns == "" {
				ns = "turtlecrawl"
			}
			paused, _ := args["paused"].(bool)
			return setKEDAPaused(name, ns, paused)
		},
	)
}
