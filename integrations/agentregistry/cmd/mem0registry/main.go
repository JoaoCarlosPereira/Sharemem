// mem0registry boots AgentRegistry with Mem0 Shared Authn/AuthZ (fail-closed).
// See ADR-005 / loja-interna-skills TechSpec.
package main

import (
	"context"
	"log/slog"
	"os"

	"github.com/agentregistry-dev/agentregistry/internal/integrations/openmemory"
	"github.com/agentregistry-dev/agentregistry/pkg/api/v1alpha1"
	"github.com/agentregistry-dev/agentregistry/pkg/registry"
	"github.com/agentregistry-dev/agentregistry/pkg/types"
)

func main() {
	ctx := context.Background()

	authn, err := openmemory.NewAuthnFromEnv(nil)
	if err != nil {
		slog.Error("mem0registry authn config", "error", err)
		os.Exit(1)
	}
	authz := openmemory.NewAuthz()
	catalogAuthorizer := openmemory.CatalogAuthorizer(authz)

	opts := types.AppOptions{
		AuthnProvider: authn,
		AuthzProvider: authz,
		Authorizers: map[string]types.Authorizer{
			v1alpha1.KindAgent:      catalogAuthorizer,
			v1alpha1.KindMCPServer:  catalogAuthorizer,
			v1alpha1.KindSkill:      catalogAuthorizer,
			v1alpha1.KindPlugin:     catalogAuthorizer,
			v1alpha1.KindHook:       catalogAuthorizer,
			v1alpha1.KindPrompt:     catalogAuthorizer,
			v1alpha1.KindModel:      catalogAuthorizer,
			v1alpha1.KindRuntime:    catalogAuthorizer,
			v1alpha1.KindDeployment: openmemory.DenyDeployAuthorizer,
		},
	}

	if err := registry.App(ctx, opts); err != nil {
		slog.Error("failed to start mem0registry", "error", err)
		os.Exit(1)
	}
}
