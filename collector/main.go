package main

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var rootCmd = &cobra.Command{
	Use:   "collector",
	Short: "turtlecrawl k8s + Prometheus tool binary",
	Long: `collector is called by the Python agent as a subprocess.
All commands output JSON to stdout. Errors are JSON with an "error" key.`,
}

func main() {
	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintf(os.Stderr, `{"error": "%s"}`+"\n", err)
		os.Exit(1)
	}
}
