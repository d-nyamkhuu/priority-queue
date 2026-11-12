{{/* Chart resources are named after the release. */}}
{{- define "pd-pool.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "pd-pool.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "pd-pool.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "pd-pool.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: priority-queue
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* <registry>/<repository>:<tag>, the image layout of the llm-d charts. Takes an image dict. */}}
{{- define "pd-pool.image" -}}
{{- printf "%s/%s:%s" .registry .repository (toString .tag) }}
{{- end }}

{{/* Labels on model-server pods: the router's selector plus llm-d's own. */}}
{{- define "pd-pool.modelServerLabels" -}}
{{- toYaml .Values.modelServers.matchLabels }}
llm-d.ai/inference-serving: "true"
llm-d.ai/engine-type: vllm
llm-d.ai/accelerator-vendor: nvidia
llm-d.ai/accelerator-variant: gpu
llm-d.ai/model: {{ .Values.model.name | splitList "/" | last | quote }}
{{- end }}

{{/*
Port vLLM listens on for a role, as in upstream llm-d: decode's vLLM is on
8200 behind the routing sidecar, which owns 8000. Takes the role name.
*/}}
{{- define "pd-pool.modelServerPort" -}}
{{- ternary 8200 8000 (eq . "decode") }}
{{- end }}

{{/* File name of the marker written once the model (at this revision) is cached. */}}
{{- define "pd-pool.weightsMarker" -}}
.ready-{{ .Values.model.name | replace "/" "--" | lower }}{{ with .Values.model.revision }}-{{ . }}{{ end }}
{{- end }}

{{/* Shell that downloads the model into HF_HOME=/hf-cache once and leaves the marker. */}}
{{- define "pd-pool.downloadCommand" -}}
set -e
marker=/hf-cache/{{ include "pd-pool.weightsMarker" . }}
if [ -f "$marker" ]; then echo "weights already cached"; exit 0; fi
hf download {{ .Values.model.name }}{{ with .Values.model.revision }} --revision {{ . }}{{ end }}
touch "$marker"
{{- end }}

{{/* Env for a container that downloads from the Hugging Face Hub. */}}
{{- define "pd-pool.hfEnv" -}}
- name: HF_HOME
  value: /hf-cache
{{- with .Values.model.hfToken.secretName }}
- name: HF_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: {{ $.Values.model.hfToken.key }}
{{- end }}
{{- end }}
