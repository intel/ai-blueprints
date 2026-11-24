{{/* Copyright (C) 2026 Intel Corporation */}}
{{/* SPDX-License-Identifier: Apache-2.0 */}}

{{- define "routes.labels" -}}
app.kubernetes.io/name: finetuning-gateway-routes
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: finetuning-as-a-service
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "routes.parentRefs" -}}
parentRefs:
  - group: gateway.networking.k8s.io
    kind: Gateway
    name: {{ .Values.platformGateway.name }}
    namespace: {{ .Values.platformGateway.namespace }}
    sectionName: {{ .Values.platformGateway.sectionName }}
hostnames:
  - {{ required "host is required" .Values.host | quote }}
{{- end -}}

{{/* A backendRef; args: dict "b" <backend values> "ns" <release namespace> */}}
{{- define "routes.backend" -}}
backendRefs:
  - name: {{ .b.service }}
    {{- if ne .b.namespace .ns }}
    namespace: {{ .b.namespace }}
    {{- end }}
    port: {{ .b.port }}
{{- end -}}

{{/*
  Identity headers a client could set itself. Envoy writes X-Forwarded-User
  from the verified token on the signed-in route (replacing a forged one); the
  rest are dropped everywhere, so nothing behind the gateway can be fooled.
*/}}
{{- define "routes.dropForged" -}}
- type: RequestHeaderModifier
  requestHeaderModifier:
    remove:
      - X-User
      - X-Userinfo
      - X-Real-IP
      {{- if .withUser }}
      - X-Forwarded-User
      {{- end }}
{{- end -}}

{{/* A rule rewriting <basePath><from> to <to>; args: dict "root" $ "from" "to" "b" ["exact"] */}}
{{- define "routes.rule" -}}
- matches:
    - path:
        type: {{ if .exact }}Exact{{ else }}PathPrefix{{ end }}
        value: {{ printf "%s%s" .root.Values.basePath .from }}
  filters:
    {{- include "routes.dropForged" (dict "withUser" .public) | nindent 4 }}
    {{- if .exact }}
    - type: URLRewrite
      urlRewrite:
        path:
          type: ReplaceFullPath
          replaceFullPath: {{ .to }}
    {{- else }}
    - type: URLRewrite
      urlRewrite:
        path:
          type: ReplacePrefixMatch
          replacePrefixMatch: {{ .to }}
    {{- end }}
  {{- include "routes.backend" (dict "b" .b "ns" .root.Release.Namespace) | nindent 2 }}
{{- end -}}
