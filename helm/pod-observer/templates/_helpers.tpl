{{/* Standard labels applied to every resource */}}
{{- define "pod-observer.labels" -}}
app.kubernetes.io/name: pod-observer
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Name of the secret actually used at runtime (existing or chart-created) */}}
{{- define "pod-observer.secretName" -}}
{{- if .Values.anthropic.existingSecret -}}
{{ .Values.anthropic.existingSecret }}
{{- else -}}
pod-observer-secrets
{{- end -}}
{{- end -}}

{{- define "pod-observer.secretKey" -}}
{{- if .Values.anthropic.existingSecret -}}
{{ .Values.anthropic.existingSecretKey }}
{{- else -}}
anthropic-api-key
{{- end -}}
{{- end -}}
