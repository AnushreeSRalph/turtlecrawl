package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"github.com/spf13/cobra"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

// k8sClient returns a kubernetes client using in-cluster config, falling back
// to local kubeconfig (for development outside the cluster).
func k8sClient() (*kubernetes.Clientset, error) {
	config, err := rest.InClusterConfig()
	if err != nil {
		// fallback to kubeconfig
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

// ─── get-replicas ─────────────────────────────────────────────────────────────

type ReplicasResult struct {
	Deployment string `json:"deployment"`
	Namespace  string `json:"namespace"`
	Replicas   int32  `json:"replicas"`
	Ready      int32  `json:"ready"`
	Available  int32  `json:"available"`
}

var (
	scaleDeployment string
	scaleNamespace  string
	scaleReplicas   int32
)

var getReplicasCmd = &cobra.Command{
	Use:   "get-replicas",
	Short: "Get current replica count for a deployment",
	Run: func(cmd *cobra.Command, args []string) {
		cs, err := k8sClient()
		if err != nil {
			jsonError(err)
			return
		}

		dep, err := cs.AppsV1().Deployments(scaleNamespace).Get(
			context.Background(), scaleDeployment, metav1.GetOptions{},
		)
		if err != nil {
			jsonError(fmt.Errorf("getting deployment: %w", err))
			return
		}

		out, _ := json.Marshal(ReplicasResult{
			Deployment: scaleDeployment,
			Namespace:  scaleNamespace,
			Replicas:   *dep.Spec.Replicas,
			Ready:      dep.Status.ReadyReplicas,
			Available:  dep.Status.AvailableReplicas,
		})
		fmt.Println(string(out))
	},
}

// ─── scale ───────────────────────────────────────────────────────────────────

type ScaleResult struct {
	Deployment  string `json:"deployment"`
	Namespace   string `json:"namespace"`
	OldReplicas int32  `json:"old_replicas"`
	NewReplicas int32  `json:"new_replicas"`
	Status      string `json:"status"`
}

var scaleCmd = &cobra.Command{
	Use:   "scale",
	Short: "Scale a deployment to a given replica count",
	Run: func(cmd *cobra.Command, args []string) {
		if scaleReplicas < 1 || scaleReplicas > 50 {
			jsonError(fmt.Errorf("replicas must be between 1 and 50, got %d", scaleReplicas))
			return
		}

		cs, err := k8sClient()
		if err != nil {
			jsonError(err)
			return
		}

		// Get current replica count first
		dep, err := cs.AppsV1().Deployments(scaleNamespace).Get(
			context.Background(), scaleDeployment, metav1.GetOptions{},
		)
		if err != nil {
			jsonError(fmt.Errorf("getting deployment: %w", err))
			return
		}
		oldReplicas := *dep.Spec.Replicas

		// Patch the scale subresource
		scale, err := cs.AppsV1().Deployments(scaleNamespace).GetScale(
			context.Background(), scaleDeployment, metav1.GetOptions{},
		)
		if err != nil {
			jsonError(fmt.Errorf("getting scale: %w", err))
			return
		}

		scale.Spec.Replicas = scaleReplicas
		_, err = cs.AppsV1().Deployments(scaleNamespace).UpdateScale(
			context.Background(), scaleDeployment, scale, metav1.UpdateOptions{},
		)
		if err != nil {
			jsonError(fmt.Errorf("updating scale: %w", err))
			return
		}

		out, _ := json.Marshal(ScaleResult{
			Deployment:  scaleDeployment,
			Namespace:   scaleNamespace,
			OldReplicas: oldReplicas,
			NewReplicas: scaleReplicas,
			Status:      "scaled",
		})
		fmt.Println(string(out))
	},
}

// ─── helpers ─────────────────────────────────────────────────────────────────

func jsonError(err error) {
	out, _ := json.Marshal(map[string]string{"error": err.Error()})
	fmt.Println(string(out))
	os.Exit(1)
}

func init() {
	// Shared flags for deployment commands
	for _, cmd := range []*cobra.Command{getReplicasCmd, scaleCmd} {
		cmd.Flags().StringVar(&scaleDeployment, "deployment", "", "Deployment name (required)")
		cmd.Flags().StringVar(&scaleNamespace, "namespace", "turtlecrawl", "Kubernetes namespace")
		_ = cmd.MarkFlagRequired("deployment")
	}

	scaleCmd.Flags().Int32Var(&scaleReplicas, "replicas", 0, "Target replica count (required, 1-50)")
	_ = scaleCmd.MarkFlagRequired("replicas")

	rootCmd.AddCommand(getReplicasCmd)
	rootCmd.AddCommand(scaleCmd)
}
