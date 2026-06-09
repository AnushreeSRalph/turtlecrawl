// nats.go — NATS JetStream tools for the MCP server.
//
// Tools:
//   get_nats_stream_info  — JetStream stream + consumer status
//   publish_metric_snapshot — push a metric snapshot to the audit subject
//
// NATS URL: $NATS_URL env var, defaults to in-cluster address.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/nats-io/nats.go"
)

const (
	// Default NATS server address inside GKE.
	defaultNATSURL = "nats://nats.turtlecrawl.svc.cluster.local:4222"

	// JetStream stream name.
	streamName = "TURTLECRAWL"

	// Subject for KEDA scaling triggers.
	subjectKEDALoad = "turtlecrawl.keda.load"

	// Subject for audit trail snapshots (→ GCS).
	subjectAuditSnapshots = "turtlecrawl.audit.snapshots"
)

// natsURL returns the NATS server URL, respecting the NATS_URL env var.
func natsURL() string {
	if u := os.Getenv("NATS_URL"); u != "" {
		return u
	}
	return defaultNATSURL
}

// natsConnect opens a NATS connection with JetStream enabled.
func natsConnect() (*nats.Conn, nats.JetStreamContext, error) {
	nc, err := nats.Connect(natsURL(),
		nats.Timeout(5*time.Second),
		nats.MaxReconnects(3),
	)
	if err != nil {
		return nil, nil, fmt.Errorf("nats connect to %s: %w", natsURL(), err)
	}
	js, err := nc.JetStream()
	if err != nil {
		nc.Close()
		return nil, nil, fmt.Errorf("nats jetstream init: %w", err)
	}
	return nc, js, nil
}

// ─── StreamInfo ──────────────────────────────────────────────────────────────

// NATSStreamInfo is returned by get_nats_stream_info.
type NATSStreamInfo struct {
	Stream    string             `json:"stream"`
	Messages  uint64             `json:"messages"`
	Bytes     uint64             `json:"bytes_stored"`
	Subjects  map[string]uint64  `json:"subjects,omitempty"`
	Consumers []NATSConsumerInfo `json:"consumers,omitempty"`
	Timestamp string             `json:"timestamp"`
}

// NATSConsumerInfo holds per-consumer lag data.
type NATSConsumerInfo struct {
	Name          string `json:"name"`
	AckPending    int    `json:"ack_pending"`   // messages delivered but not acked
	NumPending    uint64 `json:"num_pending"`   // messages waiting to be delivered (lag)
	NumRedelivered uint64 `json:"num_redelivered"`
	Delivered     uint64 `json:"delivered"`
}

func getNATSStreamInfo(stream, consumer string) (NATSStreamInfo, error) {
	nc, js, err := natsConnect()
	if err != nil {
		return NATSStreamInfo{}, err
	}
	defer nc.Close()

	if stream == "" {
		stream = streamName
	}

	si, err := js.StreamInfo(stream)
	if err != nil {
		return NATSStreamInfo{}, fmt.Errorf("stream info for %q: %w", stream, err)
	}

	info := NATSStreamInfo{
		Stream:    stream,
		Messages:  si.State.Msgs,
		Bytes:     si.State.Bytes,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}

	// NumSubjects is a uint64 count — actual per-subject breakdown requires
	// the Subjects map which is only populated when stream is created with
	// SubjectTransform or explicitly requested. Skip for now.
	_ = si.State.NumSubjects

	// If a consumer name was specified, fetch its lag details
	consumerNames := []string{}
	if consumer != "" {
		consumerNames = append(consumerNames, consumer)
	} else {
		// List all consumers
		for c := range js.ConsumerNames(stream) {
			consumerNames = append(consumerNames, c)
		}
	}

	for _, cname := range consumerNames {
		ci, err := js.ConsumerInfo(stream, cname)
		if err != nil {
			continue
		}
		info.Consumers = append(info.Consumers, NATSConsumerInfo{
			Name:           cname,
			AckPending:     ci.NumAckPending,
			NumPending:     ci.NumPending,
			NumRedelivered: uint64(ci.NumRedelivered),
			Delivered:      ci.Delivered.Consumer,
		})
	}

	return info, nil
}

// ─── PublishMetricSnapshot ────────────────────────────────────────────────────

// MetricSnapshot is the payload published to turtlecrawl.audit.snapshots.
type MetricSnapshot struct {
	Label     string                 `json:"label,omitempty"`
	Metrics   map[string]interface{} `json:"metrics"`
	Timestamp string                 `json:"timestamp"`
}

// PublishResult is returned by publish_metric_snapshot.
type PublishResult struct {
	Subject   string `json:"subject"`
	Stream    string `json:"stream"`
	Sequence  uint64 `json:"sequence"`
	Timestamp string `json:"timestamp"`
}

func publishMetricSnapshot(metrics map[string]interface{}, label string) (PublishResult, error) {
	nc, js, err := natsConnect()
	if err != nil {
		return PublishResult{}, err
	}
	defer nc.Close()

	snapshot := MetricSnapshot{
		Label:     label,
		Metrics:   metrics,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}

	payload, err := json.Marshal(snapshot)
	if err != nil {
		return PublishResult{}, fmt.Errorf("marshaling snapshot: %w", err)
	}

	ack, err := js.Publish(subjectAuditSnapshots, payload)
	if err != nil {
		return PublishResult{}, fmt.Errorf("publishing to %s: %w", subjectAuditSnapshots, err)
	}

	return PublishResult{
		Subject:   subjectAuditSnapshots,
		Stream:    ack.Stream,
		Sequence:  ack.Sequence,
		Timestamp: snapshot.Timestamp,
	}, nil
}

// ─── MCP tool registration ────────────────────────────────────────────────────

func init() {
	registerTool(
		mcpToolDef{
			Name: "get_nats_stream_info",
			Description: "Get NATS JetStream stream status: message count, bytes stored, " +
				"and per-consumer lag (num_pending). " +
				"KEDA uses consumer lag on turtlecrawl.keda.load to trigger scaling. " +
				"High num_pending means KEDA is about to scale up sample-app.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"stream": map[string]interface{}{
						"type":        "string",
						"description": "JetStream stream name (default: TURTLECRAWL)",
					},
					"consumer": map[string]interface{}{
						"type":        "string",
						"description": "Consumer name to fetch lag for (omit for all consumers)",
					},
				},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			stream, _ := args["stream"].(string)
			consumer, _ := args["consumer"].(string)
			return getNATSStreamInfo(stream, consumer)
		},
	)

	registerTool(
		mcpToolDef{
			Name: "publish_metric_snapshot",
			Description: "Publish a metric snapshot to the NATS audit subject " +
				"(turtlecrawl.audit.snapshots). The snapshot is durably stored in " +
				"JetStream and asynchronously consumed by the GCS writer. " +
				"Use this to record key moments in the scaling experiment for the audit log.",
			InputSchema: map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"metrics": map[string]interface{}{
						"type":        "object",
						"description": "Key-value map of metric names to values",
					},
					"label": map[string]interface{}{
						"type":        "string",
						"description": "Short label for this snapshot (e.g. 'pre-scale-down', 'post-stabilize')",
					},
				},
				"required": []string{"metrics"},
			},
		},
		func(args map[string]interface{}) (interface{}, error) {
			metrics, ok := args["metrics"].(map[string]interface{})
			if !ok {
				return nil, fmt.Errorf("metrics must be an object")
			}
			label, _ := args["label"].(string)
			return publishMetricSnapshot(metrics, label)
		},
	)
}
