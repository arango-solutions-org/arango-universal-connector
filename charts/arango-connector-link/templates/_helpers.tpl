{{- define "link.name" -}}{{ .Release.Name }}{{- end -}}

{{/* A stable UUID derived from the release name, unless linkId is set. */}}
{{- define "link.id" -}}
{{- if .Values.linkId -}}{{ .Values.linkId }}{{- else -}}
{{- $h := sha256sum (printf "arango-connector-link/%s/%s" .Release.Namespace .Release.Name) -}}
{{- printf "%s-%s-4%s-8%s-%s" (substr 0 8 $h) (substr 8 12 $h) (substr 13 16 $h) (substr 17 20 $h) (substr 20 32 $h) -}}
{{- end -}}
{{- end -}}

{{- define "link.schema" -}}
{{- $file := printf "schemas/%s.json" .Values.link -}}
{{- $raw := .Files.Get $file -}}
{{- if not $raw -}}{{- fail (printf "unknown link %q: no %s in the chart" .Values.link $file) -}}{{- end -}}
{{- $raw -}}
{{- end -}}
