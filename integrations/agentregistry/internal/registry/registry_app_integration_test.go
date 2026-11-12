//go:build integration

package registry

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/danielgtaylor/huma/v2/humatest"
	"github.com/stretchr/testify/require"

	internaldb "github.com/agentregistry-dev/agentregistry/internal/registry/database"
	arv0 "github.com/agentregistry-dev/agentregistry/pkg/api/v0"
	"github.com/agentregistry-dev/agentregistry/pkg/api/v1alpha1"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/artifact"
	pkgdb "github.com/agentregistry-dev/agentregistry/pkg/registry/database"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/resource"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/v1alpha1store"
	"github.com/agentregistry-dev/agentregistry/pkg/types"
	"github.com/agentregistry-dev/agentregistry/pkg/types/typestest"
)

const extensionApplyKind = "IntegrationExtension"

type extensionApplySpec struct {
	Value string `json:"value" yaml:"value"`
}

type extensionApplyObject struct {
	v1alpha1.TypeMeta `json:",inline" yaml:",inline"`
	Metadata          v1alpha1.ObjectMeta `json:"metadata" yaml:"metadata"`
	Spec              extensionApplySpec  `json:"spec" yaml:"spec"`
	Status            v1alpha1.Status     `json:"status,omitzero" yaml:"status,omitempty"`
}

func (e *extensionApplyObject) GetMetadata() *v1alpha1.ObjectMeta    { return &e.Metadata }
func (e *extensionApplyObject) SetMetadata(meta v1alpha1.ObjectMeta) { e.Metadata = meta }
func (e *extensionApplyObject) MarshalSpec() (json.RawMessage, error) {
	return json.Marshal(e.Spec)
}
func (e *extensionApplyObject) UnmarshalSpec(data json.RawMessage) error {
	return json.Unmarshal(data, &e.Spec)
}
func (e *extensionApplyObject) MarshalStatus() (json.RawMessage, error) {
	return v1alpha1.MarshalStatusForStorage(e.Status)
}
func (e *extensionApplyObject) UnmarshalStatus(data json.RawMessage) error {
	return v1alpha1.UnmarshalStatusFromStorage(data, &e.Status)
}

func TestBuildStores_ExtensionKindAppliesThroughBatchEndpoint(t *testing.T) {
	pool := v1alpha1store.NewTestPool(t)
	stores := buildStores(pool, map[string]string{
		extensionApplyKind: "agents",
	}, nil, nil)
	extensionStore := stores[extensionApplyKind]
	require.NotNil(t, extensionStore)

	scheme := v1alpha1.NewScheme()
	scheme.MustRegister(extensionApplyKind, extensionApplySpec{}, func() any { return &extensionApplyObject{} })

	_, api := humatest.New(t)
	resource.RegisterApply(api, resource.ApplyConfig{
		BasePrefix: "/v0",
		Stores:     stores,
		Scheme:     scheme,
	})

	yaml := []byte(`apiVersion: ar.dev/v1alpha1
kind: IntegrationExtension
metadata:
  name: extension-only
  tag: stable
spec:
  value: ok
`)
	resp := api.Post("/v0/apply", "Content-Type: application/yaml", strings.NewReader(string(yaml)))
	require.Equal(t, http.StatusOK, resp.Code, resp.Body.String())

	var out struct {
		Results []arv0.ApplyResult `json:"results"`
	}
	require.NoError(t, json.Unmarshal(resp.Body.Bytes(), &out))
	require.Len(t, out.Results, 1)
	require.Equal(t, extensionApplyKind, out.Results[0].Kind)
	require.Equal(t, arv0.ApplyStatusCreated, out.Results[0].Status)
	require.Equal(t, "stable", out.Results[0].Tag)

	row, err := extensionStore.Get(t.Context(), v1alpha1.DefaultNamespace, "extension-only", "stable")
	require.NoError(t, err)
	require.JSONEq(t, `{"value":"ok"}`, string(row.Spec))
}

// TestBuildStores_PropagatesAuditor verifies the auditor passed
// through buildStores (the AppOptions.Auditor field)
// reaches every constructed Store. We drive a tagged-artifact Upsert
// and assert the auditor saw the expected ResourceTagCreated event,
// proving the option survived the
// NewStores -> NewStore option chain.
func TestBuildStores_PropagatesAuditor(t *testing.T) {
	pool := v1alpha1store.NewTestPool(t)
	auditor := &typestest.RecordingAuditor{}
	stores := buildStores(pool, nil, nil, auditor)

	agentStore := stores[v1alpha1.KindAgent]
	require.NotNil(t, agentStore)

	_, err := agentStore.Upsert(t.Context(), &v1alpha1.Agent{
		TypeMeta: v1alpha1.TypeMeta{APIVersion: v1alpha1.GroupVersion, Kind: v1alpha1.KindAgent},
		Metadata: v1alpha1.ObjectMeta{Namespace: v1alpha1.DefaultNamespace, Name: "audited"},
		Spec:     v1alpha1.AgentSpec{Title: "Audited Agent"},
	})
	require.NoError(t, err)

	events := auditor.Events()
	require.Len(t, events, 1)
	require.Equal(t, v1alpha1.KindAgent, events[0].Kind)
	require.Equal(t, v1alpha1.DefaultNamespace, events[0].Namespace)
	require.Equal(t, "audited", events[0].Name)
	require.NotEmpty(t, events[0].Tag)

	// Sanity: nil auditor still works (NoopAuditor fallback) — guards the
	// nil-check branch in buildStores.
	stores2 := buildStores(pool, nil, nil, nil)
	require.NotNil(t, stores2[v1alpha1.KindAgent])
	_ = types.NoopAuditor
}

func TestLocalAppArtifactPutPinsResolvedSource(t *testing.T) {
	pool := v1alpha1store.NewTestPool(t)
	stores := buildStores(pool, nil, nil, nil)
	_, err := stores[v1alpha1.KindLocalApp].Upsert(t.Context(), &v1alpha1.LocalApp{
		TypeMeta: v1alpha1.TypeMeta{APIVersion: v1alpha1.GroupVersion, Kind: v1alpha1.KindLocalApp},
		Metadata: v1alpha1.ObjectMeta{Namespace: v1alpha1.DefaultNamespace, Name: "horas-app", Tag: "1.0.0"},
		Spec: v1alpha1.LocalAppSpec{
			Description:    "Lanca as horas",
			Usage:          "horas-app lancar",
			Effects:        []string{"escrita-externa"},
			Area:           "gestao",
			Wheel:          "horas_app-1.0.0-py3-none-any.whl",
			PythonRequires: ">=3.11",
			Commands:       v1alpha1.LocalAppCommands{Start: "horas-app", Verify: "horas-app verificar"},
		},
	})
	require.NoError(t, err)

	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for name, body := range map[string]string{"APP.md": "# App", "horas_app-1.0.0-py3-none-any.whl": "PK"} {
		require.NoError(t, tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(body)), Typeflag: tar.TypeReg}))
		_, err := tw.Write([]byte(body))
		require.NoError(t, err)
	}
	require.NoError(t, tw.Close())
	require.NoError(t, gz.Close())

	sch := pkgdb.OSSSchemaRegistry().MustGet(pkgdb.OSSSourceName)
	artifactStore := internaldb.NewPostgresLocalAppArtifactStore(pool, sch)
	_, transport := artifactTransports(pool, stores)
	require.NotNil(t, transport)
	key := types.SkillArtifactKey{Namespace: v1alpha1.DefaultNamespace, Name: "horas-app", Tag: "1.0.0"}
	_, err = transport.Put(t.Context(), key, bytes.NewReader(buf.Bytes()), types.SkillArtifactPutOptions{Size: -1})
	require.NoError(t, err)

	row, err := stores[v1alpha1.KindLocalApp].Get(t.Context(), key.Namespace, key.Name, key.Tag)
	require.NoError(t, err)
	app := &v1alpha1.LocalApp{}
	require.NoError(t, app.UnmarshalStatus(row.Status))
	stored, err := artifactStore.Get(t.Context(), artifact.SkillRef{Namespace: key.Namespace, Name: key.Name, Tag: key.Tag})
	require.NoError(t, err)
	require.NotNil(t, app.Status.ResolvedSource)
	require.NotNil(t, app.Status.ResolvedSource.Artifact)
	sum := sha256.Sum256(buf.Bytes())
	require.Equal(t, hex.EncodeToString(sum[:]), stored.Digest)
	require.Equal(t, v1alpha1.LocalAppResolvedArtifact{
		Digest: stored.Digest, MediaType: artifact.LocalAppMediaTypeTarGzip, Size: int64(buf.Len()),
	}, *app.Status.ResolvedSource.Artifact)
	require.True(t, app.Status.IsConditionTrue("Ready"))
}
