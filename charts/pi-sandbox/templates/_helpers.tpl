{{- define "pi.fullname" -}}
{{- printf "%s-pi" .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- define "pi.labels" -}}
app.kubernetes.io/name: pi-sandbox
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
