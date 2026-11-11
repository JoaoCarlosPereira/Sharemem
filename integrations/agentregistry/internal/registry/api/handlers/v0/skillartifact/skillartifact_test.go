package skillartifact_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"io"
	"net/http"
	"testing"

	"github.com/danielgtaylor/huma/v2"
	"github.com/danielgtaylor/huma/v2/humatest"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/agentregistry-dev/agentregistry/internal/registry/api/handlers/v0/skillartifact"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/artifact"
	pkgdb "github.com/agentregistry-dev/agentregistry/pkg/registry/database"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/resource"
	"github.com/agentregistry-dev/agentregistry/pkg/types"
)

type memoryStore struct {
	artifact types.SkillArtifact
	putKey   types.SkillArtifactKey
	putOpts  types.SkillArtifactPutOptions
	putBody  []byte
	putErr   error
	getErr   error
}

func (s *memoryStore) Put(_ context.Context, key types.SkillArtifactKey, r io.Reader, opts types.SkillArtifactPutOptions) (types.SkillArtifact, error) {
	s.putKey = key
	s.putOpts = opts
	s.putBody, _ = io.ReadAll(r)
	if s.putErr != nil {
		return types.SkillArtifact{}, s.putErr
	}
	return types.SkillArtifact{Size: opts.Size, SHA256: opts.SHA256}, nil
}

func (s *memoryStore) Get(_ context.Context, _ types.SkillArtifactKey) (types.SkillArtifact, error) {
	if s.getErr != nil {
		return types.SkillArtifact{}, s.getErr
	}
	return s.artifact, nil
}

func TestPutArtifactValidatesDigestAndAuthorizesUpdate(t *testing.T) {
	store := &memoryStore{}
	var authorized resource.AuthorizeInput
	_, api := humatest.New(t)
	skillartifact.Register(api, skillartifact.Config{
		BasePrefix: "/v0",
		Store:      store,
		Authorize: func(_ context.Context, in resource.AuthorizeInput) error {
			authorized = in
			return nil
		},
	})
	body := []byte("tar-gzip-content")
	sum := sha256.Sum256(body)
	digest := "sha-256=" + base64.StdEncoding.EncodeToString(sum[:])

	resp := api.Put("/v0/skills/code-review/v1/artifact?namespace=team-a",
		"Content-Type: "+skillartifact.MediaType,
		"Digest: "+digest,
		bytes.NewReader(body))

	require.Equal(t, http.StatusCreated, resp.Code, resp.Body.String())
	assert.Equal(t, "update", authorized.Verb)
	assert.Equal(t, "Skill", authorized.Kind)
	assert.Equal(t, types.SkillArtifactKey{Namespace: "team-a", Name: "code-review", Tag: "v1"}, store.putKey)
	assert.Equal(t, body, store.putBody)
	assert.Equal(t, int64(len(body)), store.putOpts.Size)
	assert.Equal(t, sum[:], store.putOpts.SHA256)
	assert.Equal(t, digest, resp.Header().Get("Digest"))
	assert.NotEmpty(t, resp.Header().Get("ETag"))
}

func TestPutArtifactRejectsDigestMismatch(t *testing.T) {
	store := &memoryStore{}
	_, api := humatest.New(t)
	skillartifact.Register(api, skillartifact.Config{BasePrefix: "/v0", Store: store})
	wrong := sha256.Sum256([]byte("different"))

	resp := api.Put("/v0/skills/code-review/v1/artifact",
		"Content-Type: "+skillartifact.MediaType,
		"Digest: sha-256="+base64.StdEncoding.EncodeToString(wrong[:]),
		bytes.NewReader([]byte("actual")))

	assert.Equal(t, http.StatusUnprocessableEntity, resp.Code, resp.Body.String())
	assert.Nil(t, store.putBody)
}

func TestPutArtifactMapsConflict(t *testing.T) {
	store := &memoryStore{putErr: pkgdb.ErrAlreadyExists}
	_, api := humatest.New(t)
	skillartifact.Register(api, skillartifact.Config{BasePrefix: "/v0", Store: store})

	resp := api.Put("/v0/skills/code-review/v1/artifact",
		"Content-Type: "+skillartifact.MediaType,
		bytes.NewReader([]byte("artifact")))

	assert.Equal(t, http.StatusConflict, resp.Code, resp.Body.String())
}

func TestGetArtifactStreamsHeadersAndAuthorizesRead(t *testing.T) {
	body := []byte("stored-artifact")
	sum := sha256.Sum256(body)
	store := &memoryStore{artifact: types.SkillArtifact{
		Content: io.NopCloser(bytes.NewReader(body)),
		Size:    int64(len(body)),
		SHA256:  sum[:],
	}}
	var authorized resource.AuthorizeInput
	_, api := humatest.New(t)
	skillartifact.Register(api, skillartifact.Config{
		BasePrefix: "/v0",
		Store:      store,
		Authorize: func(_ context.Context, in resource.AuthorizeInput) error {
			authorized = in
			return nil
		},
	})

	resp := api.Get("/v0/skills/code-review/v1/artifact")

	require.Equal(t, http.StatusOK, resp.Code, resp.Body.String())
	assert.Equal(t, body, resp.Body.Bytes())
	assert.Equal(t, "get", authorized.Verb)
	assert.Equal(t, skillartifact.MediaType, resp.Header().Get("Content-Type"))
	assert.Equal(t, "attachment; filename=code-review-v1.tar.gz", resp.Header().Get("Content-Disposition"))
	assert.Equal(t, "15", resp.Header().Get("Content-Length"))
	assert.Equal(t, "nosniff", resp.Header().Get("X-Content-Type-Options"))
	assert.Equal(t, "sha-256="+base64.StdEncoding.EncodeToString(sum[:]), resp.Header().Get("Digest"))
	assert.NotEmpty(t, resp.Header().Get("ETag"))
}

func TestGetArtifactMapsErrorsAndAuthorization(t *testing.T) {
	tests := []struct {
		name      string
		storeErr  error
		authorize func(context.Context, resource.AuthorizeInput) error
		want      int
	}{
		{name: "not found", storeErr: pkgdb.ErrNotFound, want: http.StatusNotFound},
		{name: "store failure", storeErr: errors.New("boom"), want: http.StatusInternalServerError},
		{name: "forbidden", authorize: func(context.Context, resource.AuthorizeInput) error {
			return huma.Error403Forbidden("denied")
		}, want: http.StatusForbidden},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, api := humatest.New(t)
			skillartifact.Register(api, skillartifact.Config{
				BasePrefix: "/v0",
				Store:      &memoryStore{getErr: tt.storeErr},
				Authorize:  tt.authorize,
			})
			resp := api.Get("/v0/skills/code-review/v1/artifact")
			assert.Equal(t, tt.want, resp.Code, resp.Body.String())
		})
	}
}

func TestDownloadSkillPackageReturnsCompleteZip(t *testing.T) {
	tarGz, err := artifact.PackFiles(map[string][]byte{
		"SKILL.md":           []byte("# Skill em português"),
		"references/guia.md": []byte("Conteúdo da referência."),
	})
	require.NoError(t, err)
	store := &memoryStore{artifact: types.SkillArtifact{
		Content: io.NopCloser(bytes.NewReader(tarGz)),
		Size:    int64(len(tarGz)),
		SHA256:  func() []byte { sum := sha256.Sum256(tarGz); return sum[:] }(),
	}}
	_, api := humatest.New(t)
	skillartifact.Register(api, skillartifact.Config{BasePrefix: "/v0", Store: store})

	resp := api.Get("/v0/skills/demo/v1/download")
	require.Equal(t, http.StatusOK, resp.Code, resp.Body.String())
	assert.Equal(t, "application/zip", resp.Header().Get("Content-Type"))
	assert.Contains(t, resp.Header().Get("Content-Disposition"), "demo-v1.zip")
	assert.Greater(t, len(resp.Body.Bytes()), 0)
}

func TestLocalAppMediaTypeMatchesTheArtifactContract(t *testing.T) {
	assert.Equal(t, "application/vnd.agentregistry.localapp.v1.tar+gzip", skillartifact.LocalAppMediaType)
	assert.Equal(t, artifact.LocalAppMediaTypeTarGzip, skillartifact.LocalAppMediaType)
}
