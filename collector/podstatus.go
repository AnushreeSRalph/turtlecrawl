package main

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PodInfo summarises the readiness of a single pod.
type PodInfo struct {
	Name      string `json:"name"`
	Phase     string `json:"phase"`
	Ready     bool   `json:"ready"`
	Restarts  int32  `json:"restarts"`
	NodeName  string `json:"node"`
}

// PodStatusResult is the full JSON output of the pod-status command.
type PodStatusResult struct {
	Deployment  string    `json:"deployment"`
	Namespace   string    `json:"namespace"`
	TotalPods   int       `json:"total_pods"`
	ReadyPods   int       `json:"ready_pods"`
	AllReady    bool      `json:"all_ready"`
	Pods        []PodInfo `json:"pods"`
}

var (
	podStatusDeployment string
	podStatusNamespace  string
)

// getPodStatus is the shared implementation used by both the CLI command
// and the MCP tool handler.
func getPodStatus(deployment, namespace string) (PodStatusResult, error) {
	cs, err := k8sClient()
	if err != nil {
		return PodStatusResult{}, err
	}

	dep, err := cs.AppsV1().Deployments(namespace).Get(
		context.Background(), deployment, metav1.GetOptions{},
	)
	if err != nil {
		return PodStatusResult{}, fmt.Errorf("getting deployment: %w", err)
	}

	selector := metav1.FormatLabelSelector(dep.Spec.Selector)
	podList, err := cs.CoreV1().Pods(namespace).List(
		context.Background(), metav1.ListOptions{LabelSelector: selector},
	)
	if err != nil {
		return PodStatusResult{}, fmt.Errorf("listing pods: %w", err)
	}

	var pods []PodInfo
	readyCount := 0
	for _, pod := range podList.Items {
		ready := false
		var restarts int32
		for _, cs := range pod.Status.ContainerStatuses {
			restarts += cs.RestartCount
			if cs.Ready {
				ready = true
			}
		}
		if ready {
			readyCount++
		}
		pods = append(pods, PodInfo{
			Name:     pod.Name,
			Phase:    string(pod.Status.Phase),
			Ready:    ready,
			Restarts: restarts,
			NodeName: pod.Spec.NodeName,
		})
	}

	return PodStatusResult{
		Deployment: deployment,
		Namespace:  namespace,
		TotalPods:  len(pods),
		ReadyPods:  readyCount,
		AllReady:   readyCount == len(pods) && len(pods) > 0,
		Pods:       pods,
	}, nil
}

var podStatusCmd = &cobra.Command{
	Use:   "pod-status",
	Short: "Return readiness status for all pods of a deployment",
	Run: func(cmd *cobra.Command, args []string) {
		cs, err := k8sClient()
		if err != nil {
			jsonError(err)
			return
		}

		// Get deployment to find label selector
		dep, err := cs.AppsV1().Deployments(podStatusNamespace).Get(
			context.Background(), podStatusDeployment, metav1.GetOptions{},
		)
		if err != nil {
			jsonError(fmt.Errorf("getting deployment: %w", err))
			return
		}

		selector := metav1.FormatLabelSelector(dep.Spec.Selector)
		podList, err := cs.CoreV1().Pods(podStatusNamespace).List(
			context.Background(), metav1.ListOptions{LabelSelector: selector},
		)
		if err != nil {
			jsonError(fmt.Errorf("listing pods: %w", err))
			return
		}

		var pods []PodInfo
		readyCount := 0

		for _, pod := range podList.Items {
			ready := false
			var restarts int32
			for _, cs := range pod.Status.ContainerStatuses {
				restarts += cs.RestartCount
				if cs.Ready {
					ready = true
				}
			}
			if ready {
				readyCount++
			}
			pods = append(pods, PodInfo{
				Name:     pod.Name,
				Phase:    string(pod.Status.Phase),
				Ready:    ready,
				Restarts: restarts,
				NodeName: pod.Spec.NodeName,
			})
		}

		result := PodStatusResult{
			Deployment: podStatusDeployment,
			Namespace:  podStatusNamespace,
			TotalPods:  len(pods),
			ReadyPods:  readyCount,
			AllReady:   readyCount == len(pods) && len(pods) > 0,
			Pods:       pods,
		}

		out, _ := json.Marshal(result)
		fmt.Println(string(out))
	},
}

func init() {
	podStatusCmd.Flags().StringVar(&podStatusDeployment, "deployment", "", "Deployment name (required)")
	podStatusCmd.Flags().StringVar(&podStatusNamespace, "namespace", "turtlecrawl", "Kubernetes namespace")
	_ = podStatusCmd.MarkFlagRequired("deployment")

	rootCmd.AddCommand(podStatusCmd)
}
