package skillartifact_test

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"sort"
	"testing"

	"github.com/danielgtaylor/huma/v2"
	"github.com/danielgtaylor/huma/v2/humatest"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/agentregistry-dev/agentregistry/internal/registry/api/handlers/v0/skillartifact"
	internaldb "github.com/agentregistry-dev/agentregistry/internal/registry/database"
	"github.com/agentregistry-dev/agentregistry/pkg/api/v1alpha1"
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

type validatingArtifactStore struct {
	requiredRootFile string
	mediaType        string
	archive          []byte
}

func (s *validatingArtifactStore) Put(_ context.Context, _ artifact.SkillRef, archive []byte) (artifact.Descriptor, error) {
	if _, err := artifact.ValidatePackage(archive, s.requiredRootFile); err != nil {
		return artifact.Descriptor{}, err
	}
	s.archive = append([]byte(nil), archive...)
	sum := sha256.Sum256(archive)
	return artifact.Descriptor{Digest: hex.EncodeToString(sum[:]), MediaType: s.mediaType, Size: int64(len(archive))}, nil
}

func (s *validatingArtifactStore) Get(context.Context, artifact.SkillRef) (*artifact.Artifact, error) {
	return nil, pkgdb.ErrNotFound
}

func (s *validatingArtifactStore) Open(context.Context, artifact.SkillRef) (artifact.Descriptor, io.ReadCloser, error) {
	if s.archive == nil {
		return artifact.Descriptor{}, nil, pkgdb.ErrNotFound
	}
	sum := sha256.Sum256(s.archive)
	return artifact.Descriptor{Digest: hex.EncodeToString(sum[:]), MediaType: s.mediaType, Size: int64(len(s.archive))},
		io.NopCloser(bytes.NewReader(s.archive)), nil
}

func (s *validatingArtifactStore) ListFiles(context.Context, artifact.SkillRef) ([]artifact.File, error) {
	return nil, pkgdb.ErrNotFound
}

func packFiles(files map[string][]byte) ([]byte, error) {
	names := make([]string, 0, len(files))
	for name := range files {
		names = append(names, name)
	}
	sort.Strings(names)
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for _, name := range names {
		if err := tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(files[name])), Typeflag: tar.TypeReg}); err != nil {
			return nil, err
		}
		if _, err := tw.Write(files[name]); err != nil {
			return nil, err
		}
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}
	if err := gz.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func registerLocalAppArtifacts(t *testing.T) (humatest.TestAPI, *validatingArtifactStore, *resource.AuthorizeInput) {
	t.Helper()
	store := &validatingArtifactStore{requiredRootFile: artifact.LocalAppRootFile, mediaType: artifact.LocalAppMediaTypeTarGzip}
	authorized := &resource.AuthorizeInput{}
	_, api := humatest.New(t)
	skillartifact.Register(api, skillartifact.Config{
		BasePrefix: "/v0",
		Store:      internaldb.NewSkillArtifactTransportStore(store, nil),
		Authorize: func(_ context.Context, in resource.AuthorizeInput) error {
			*authorized = in
			return nil
		},
		Kind:              v1alpha1.KindLocalApp,
		Plural:            v1alpha1.PluralFor(v1alpha1.KindLocalApp),
		MediaType:         skillartifact.LocalAppMediaType,
		Label:             v1alpha1.KindLocalApp,
		RequiredRootFiles: []string{artifact.LocalAppRootFile},
	})
	return api, store, authorized
}

func TestLocalAppArtifactPutAcceptsPackageWithAppMd(t *testing.T) {
	api, store, authorized := registerLocalAppArtifacts(t)
	archive, err := packFiles(map[string][]byte{"APP.md": []byte("# App local"), "bin/app.sh": []byte("echo ok")})
	require.NoError(t, err)

	resp := api.Put("/v0/localapps/x/1/artifact",
		"Content-Type: "+skillartifact.LocalAppMediaType,
		bytes.NewReader(archive))

	require.Equal(t, http.StatusCreated, resp.Code, resp.Body.String())
	assert.Equal(t, archive, store.archive)
	assert.Equal(t, "update", authorized.Verb)
	assert.Equal(t, v1alpha1.KindLocalApp, authorized.Kind)
	assert.Equal(t, "x", authorized.Name)
	assert.Equal(t, "1", authorized.Tag)
}

func TestLocalAppArtifactPutRejectsHookMediaType(t *testing.T) {
	api, store, _ := registerLocalAppArtifacts(t)
	archive, err := packFiles(map[string][]byte{"APP.md": []byte("# App local")})
	require.NoError(t, err)

	resp := api.Put("/v0/localapps/x/1/artifact",
		"Content-Type: "+skillartifact.HookMediaType,
		bytes.NewReader(archive))

	assert.Equal(t, http.StatusUnsupportedMediaType, resp.Code, resp.Body.String())
	assert.Nil(t, store.archive)
}

func TestLocalAppArtifactPutRejectsPackageWithoutAppMd(t *testing.T) {
	api, store, _ := registerLocalAppArtifacts(t)
	archive, err := packFiles(map[string][]byte{"HOOK.md": []byte("# Hook"), "SKILL.md": []byte("# Skill")})
	require.NoError(t, err)

	resp := api.Put("/v0/localapps/x/1/artifact",
		"Content-Type: "+skillartifact.LocalAppMediaType,
		bytes.NewReader(archive))

	assert.Equal(t, http.StatusUnprocessableEntity, resp.Code, resp.Body.String())
	assert.Nil(t, store.archive)
}

func TestLocalAppArtifactGetReturnsSameBytesWithLocalAppContentType(t *testing.T) {
	api, _, authorized := registerLocalAppArtifacts(t)
	archive, err := packFiles(map[string][]byte{"APP.md": []byte("# App local"), "bin/app.sh": []byte("echo ok")})
	require.NoError(t, err)
	put := api.Put("/v0/localapps/x/1/artifact",
		"Content-Type: "+skillartifact.LocalAppMediaType,
		bytes.NewReader(archive))
	require.Equal(t, http.StatusCreated, put.Code, put.Body.String())
	sum := sha256.Sum256(archive)

	resp := api.Get("/v0/localapps/x/1/artifact")

	require.Equal(t, http.StatusOK, resp.Code, resp.Body.String())
	assert.Equal(t, archive, resp.Body.Bytes())
	assert.Equal(t, artifact.LocalAppMediaTypeTarGzip, resp.Header().Get("Content-Type"))
	assert.Equal(t, "attachment; filename=x-1.tar.gz", resp.Header().Get("Content-Disposition"))
	assert.Equal(t, "sha-256="+base64.StdEncoding.EncodeToString(sum[:]), resp.Header().Get("Digest"))
	assert.Equal(t, "get", authorized.Verb)
	assert.Equal(t, v1alpha1.KindLocalApp, authorized.Kind)
}

func TestLocalAppArtifactPutAcceptsMediaTypeParameters(t *testing.T) {
	api, store, _ := registerLocalAppArtifacts(t)
	archive, err := packFiles(map[string][]byte{"APP.md": []byte("# App local")})
	require.NoError(t, err)

	resp := api.Put("/v0/localapps/x/1/artifact",
		"Content-Type: "+skillartifact.LocalAppMediaType+"; charset=binary",
		bytes.NewReader(archive))

	require.Equal(t, http.StatusCreated, resp.Code, resp.Body.String())
	assert.Equal(t, archive, store.archive)
}

func TestLocalAppArtifactPutRejectsMissingContentType(t *testing.T) {
	api, store, _ := registerLocalAppArtifacts(t)
	archive, err := packFiles(map[string][]byte{"APP.md": []byte("# App local")})
	require.NoError(t, err)

	resp := api.Put("/v0/localapps/x/1/artifact", bytes.NewReader(archive))

	assert.Equal(t, http.StatusUnsupportedMediaType, resp.Code, resp.Body.String())
	assert.Nil(t, store.archive)
}

func TestSkillArtifactPutRejectsHookMediaType(t *testing.T) {
	store := &memoryStore{}
	_, api := humatest.New(t)
	skillartifact.Register(api, skillartifact.Config{BasePrefix: "/v0", Store: store})

	resp := api.Put("/v0/skills/code-review/v1/artifact",
		"Content-Type: "+skillartifact.HookMediaType,
		bytes.NewReader([]byte("artifact")))

	assert.Equal(t, http.StatusUnsupportedMediaType, resp.Code, resp.Body.String())
	assert.Nil(t, store.putBody)
}

func registerHookArtifacts(t *testing.T) (humatest.TestAPI, *memoryStore) {
	t.Helper()
	store := &memoryStore{}
	_, api := humatest.New(t)
	skillartifact.Register(api, skillartifact.Config{
		BasePrefix:        "/v0",
		Store:             store,
		Kind:              v1alpha1.KindHook,
		Plural:            v1alpha1.PluralFor(v1alpha1.KindHook),
		MediaType:         skillartifact.HookMediaType,
		Label:             v1alpha1.KindHook,
		RequiredRootFiles: []string{artifact.HookRootFile},
	})
	return api, store
}

func TestHookArtifactPutAcceptsHookMediaType(t *testing.T) {
	api, store := registerHookArtifacts(t)

	resp := api.Put("/v0/hooks/h/1/artifact",
		"Content-Type: "+skillartifact.HookMediaType,
		bytes.NewReader([]byte("artifact")))

	require.Equal(t, http.StatusCreated, resp.Code, resp.Body.String())
	assert.Equal(t, []byte("artifact"), store.putBody)
}

func TestHookArtifactPutRejectsSkillMediaType(t *testing.T) {
	api, store := registerHookArtifacts(t)

	resp := api.Put("/v0/hooks/h/1/artifact",
		"Content-Type: "+skillartifact.MediaType,
		bytes.NewReader([]byte("artifact")))

	assert.Equal(t, http.StatusUnsupportedMediaType, resp.Code, resp.Body.String())
	assert.Nil(t, store.putBody)
}

func TestLocalAppDownloadReturnsZipWithAppMd(t *testing.T) {
	api, _, authorized := registerLocalAppArtifacts(t)
	archive, err := packFiles(map[string][]byte{"APP.md": []byte("# App local"), "bin/app.sh": []byte("echo ok")})
	require.NoError(t, err)
	put := api.Put("/v0/localapps/x/1/artifact",
		"Content-Type: "+skillartifact.LocalAppMediaType,
		bytes.NewReader(archive))
	require.Equal(t, http.StatusCreated, put.Code, put.Body.String())

	resp := api.Get("/v0/localapps/x/1/download")

	require.Equal(t, http.StatusOK, resp.Code, resp.Body.String())
	assert.Equal(t, "application/zip", resp.Header().Get("Content-Type"))
	assert.Equal(t, "attachment; filename=x-1.zip", resp.Header().Get("Content-Disposition"))
	assert.Equal(t, "get", authorized.Verb)
	assert.Equal(t, v1alpha1.KindLocalApp, authorized.Kind)
	zr, err := zip.NewReader(bytes.NewReader(resp.Body.Bytes()), int64(resp.Body.Len()))
	require.NoError(t, err)
	contents := map[string]string{}
	for _, f := range zr.File {
		rc, err := f.Open()
		require.NoError(t, err)
		data, err := io.ReadAll(rc)
		require.NoError(t, err)
		require.NoError(t, rc.Close())
		contents[f.Name] = string(data)
	}
	assert.Equal(t, map[string]string{"APP.md": "# App local", "bin/app.sh": "echo ok"}, contents)
}

func TestLocalAppDownloadFailsWhenStoredPackageLacksAppMd(t *testing.T) {
	archive, err := packFiles(map[string][]byte{"SKILL.md": []byte("# Skill")})
	require.NoError(t, err)
	store := &memoryStore{artifact: types.SkillArtifact{
		Content: io.NopCloser(bytes.NewReader(archive)),
		Size:    int64(len(archive)),
		SHA256:  func() []byte { sum := sha256.Sum256(archive); return sum[:] }(),
	}}
	_, api := humatest.New(t)
	skillartifact.Register(api, skillartifact.Config{
		BasePrefix:        "/v0",
		Store:             store,
		Kind:              v1alpha1.KindLocalApp,
		Plural:            v1alpha1.PluralFor(v1alpha1.KindLocalApp),
		MediaType:         skillartifact.LocalAppMediaType,
		Label:             v1alpha1.KindLocalApp,
		RequiredRootFiles: []string{artifact.LocalAppRootFile},
	})

	resp := api.Get("/v0/localapps/x/1/download")

	assert.Equal(t, http.StatusInternalServerError, resp.Code, resp.Body.String())
}
