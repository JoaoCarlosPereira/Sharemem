package database

import (
	"reflect"
	"testing"

	"github.com/agentregistry-dev/agentregistry/pkg/registry/artifact"
	pkgdb "github.com/agentregistry-dev/agentregistry/pkg/registry/database"
)

func TestArtifactStoresUseKindSpecificPackageContract(t *testing.T) {
	schema := pkgdb.MustNewSchema("registry_test")

	skill := NewPostgresArtifactStore(nil, schema)
	if !reflect.DeepEqual(skill.requiredRootFiles, []string{artifact.SkillRootFile}) || skill.mediaType != artifact.MediaTypeTarGzip {
		t.Fatalf("unexpected Skill package contract: roots=%q mediaType=%q", skill.requiredRootFiles, skill.mediaType)
	}

	hook := NewPostgresHookArtifactStore(nil, schema)
	if !reflect.DeepEqual(hook.requiredRootFiles, []string{artifact.HookRootFile}) || hook.mediaType != artifact.HookMediaTypeTarGzip {
		t.Fatalf("unexpected Hook package contract: roots=%q mediaType=%q", hook.requiredRootFiles, hook.mediaType)
	}

	plugin := NewPostgresPluginArtifactStore(nil, schema)
	if !reflect.DeepEqual(plugin.requiredRootFiles, []string{artifact.PluginRootFile, artifact.PluginReadmeFile}) || plugin.mediaType != artifact.PluginMediaTypeTarGzip {
		t.Fatalf("unexpected Plugin package contract: roots=%q mediaType=%q", plugin.requiredRootFiles, plugin.mediaType)
	}
}
