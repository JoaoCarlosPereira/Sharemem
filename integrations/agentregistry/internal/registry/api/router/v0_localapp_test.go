package router

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"testing"

	"github.com/danielgtaylor/huma/v2/humatest"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/agentregistry-dev/agentregistry/internal/registry/api/handlers/v0/crud"
	"github.com/agentregistry-dev/agentregistry/internal/registry/api/handlers/v0/skillartifact"
	"github.com/agentregistry-dev/agentregistry/pkg/api/v1alpha1"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/resource"
	"github.com/agentregistry-dev/agentregistry/pkg/types"
)

type recordingArtifactStore struct{ putBody []byte }

func (s *recordingArtifactStore) Put(_ context.Context, _ types.SkillArtifactKey, r io.Reader, opts types.SkillArtifactPutOptions) (types.SkillArtifact, error) {
	s.putBody, _ = io.ReadAll(r)
	return types.SkillArtifact{Size: opts.Size, SHA256: opts.SHA256}, nil
}

func (s *recordingArtifactStore) Get(context.Context, types.SkillArtifactKey) (types.SkillArtifact, error) {
	return types.SkillArtifact{}, nil
}

func TestRegisterKindRoutesServesLocalAppArtifactOnlyWithStore(t *testing.T) {
	store := &recordingArtifactStore{}
	var authorized resource.AuthorizeInput
	perKind := crud.PerKindHooks{Authorizers: map[string]func(context.Context, resource.AuthorizeInput) error{
		v1alpha1.KindLocalApp: func(_ context.Context, in resource.AuthorizeInput) error {
			authorized = in
			return nil
		},
	}}
	_, withStore := humatest.New(t)
	registerKindRoutes(withStore, "/v0", Stores{}, nil, perKind, nil, nil, nil, nil, nil, nil, nil, store)
	_, withoutStore := humatest.New(t)
	registerKindRoutes(withoutStore, "/v0", Stores{}, nil, perKind, nil, nil, nil, nil, nil, nil, nil, nil)

	resp := withStore.Put("/v0/localapps/x/1/artifact",
		"Content-Type: "+skillartifact.LocalAppMediaType,
		bytes.NewReader([]byte("artifact")))
	missing := withoutStore.Put("/v0/localapps/x/1/artifact",
		"Content-Type: "+skillartifact.LocalAppMediaType,
		bytes.NewReader([]byte("artifact")))

	require.Equal(t, http.StatusCreated, resp.Code, resp.Body.String())
	assert.Equal(t, []byte("artifact"), store.putBody)
	assert.Equal(t, v1alpha1.KindLocalApp, authorized.Kind)
	assert.Equal(t, http.StatusNotFound, missing.Code, missing.Body.String())
}
