{{/*
Expand the name of the chart.
*/}}
{{- define "turtlecrawl.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Namespace to deploy into.
*/}}
{{- define "turtlecrawl.namespace" -}}
{{- .Values.namespace.name }}
{{- end }}

{{/*
Artifact Registry base URL.
Derives from global.projectId + global.region unless global.registry is set.
*/}}
{{- define "turtlecrawl.registry" -}}
{{- if .Values.global.registry -}}
{{- .Values.global.registry -}}
{{- else -}}
{{- printf "%s-docker.pkg.dev/%s/turtlecrawl" .Values.global.region .Values.global.projectId -}}
{{- end -}}
{{- end }}

{{/*
sample-app image
*/}}
{{- define "turtlecrawl.sampleApp.image" -}}
{{- if .Values.sampleApp.image.repository -}}
{{- printf "%s:%s" .Values.sampleApp.image.repository .Values.sampleApp.image.tag -}}
{{- else -}}
{{- printf "%s/sample-app:%s" (include "turtlecrawl.registry" .) .Values.sampleApp.image.tag -}}
{{- end -}}
{{- end }}

{{/*
agent image
*/}}
{{- define "turtlecrawl.agent.image" -}}
{{- if .Values.agent.image.repository -}}
{{- printf "%s:%s" .Values.agent.image.repository .Values.agent.image.tag -}}
{{- else -}}
{{- printf "%s/agent:%s" (include "turtlecrawl.registry" .) .Values.agent.image.tag -}}
{{- end -}}
{{- end }}

{{/*
NATS in-cluster URL
*/}}
{{- define "turtlecrawl.nats.url" -}}
{{- printf "nats://nats.%s.svc.cluster.local:4222" (include "turtlecrawl.namespace" .) -}}
{{- end }}

{{/*
NATS monitoring endpoint (for KEDA)
*/}}
{{- define "turtlecrawl.nats.monitoringEndpoint" -}}
{{- printf "nats.%s.svc.cluster.local:8222" (include "turtlecrawl.namespace" .) -}}
{{- end }}

{{/*
GCP service account email
*/}}
{{- define "turtlecrawl.gcpServiceAccountEmail" -}}
{{- printf "%s@%s.iam.gserviceaccount.com" .Values.rbac.gcpServiceAccount .Values.global.projectId -}}
{{- end }}

{{/*
Common labels
*/}}
{{- define "turtlecrawl.labels" -}}
app.kubernetes.io/part-of: turtlecrawl
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
