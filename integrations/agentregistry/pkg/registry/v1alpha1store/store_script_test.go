//go:build integration

package v1alpha1store_test

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/agentregistry-dev/agentregistry/pkg/api/v1alpha1"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/v1alpha1store"
)

func scriptObj(name, tag, content string) *v1alpha1.Script {
	return &v1alpha1.Script{
		TypeMeta: v1alpha1.TypeMeta{APIVersion: v1alpha1.GroupVersion, Kind: v1alpha1.KindScript},
		Metadata: v1alpha1.ObjectMeta{Namespace: "default", Name: name, Tag: tag},
		Spec:     v1alpha1.ScriptSpec{FileName: "gcenfe02_xml.py", Content: content},
	}
}

func setupScriptStoreWithVersions(t *testing.T) *v1alpha1store.Store {
	t.Helper()
	pool := v1alpha1store.NewTestPool(t)
	store := v1alpha1store.NewStores(pool, v1alpha1store.TestSchemaRegistry())[v1alpha1.KindScript]
	require.NotNil(t, store)

	ctx := context.Background()
	for _, obj := range []*v1alpha1.Script{
		scriptObj("gcenfe02-xml", "1", "print('versao 1')\r\n"),
		scriptObj("gcenfe02-xml", "2", "print('versao 2')\r\n"),
		scriptObj("gcenfe02-xml", "latest", "print('versao 2')\r\n"),
		scriptObj("outro-script", "1", "print('outro')\n"),
		scriptObj("outro-script", "latest", "print('outro')\n"),
	} {
		_, err := store.Upsert(ctx, obj)
		require.NoError(t, err)
	}
	return store
}

func TestScriptStore_GetPinnedTagReturnsThatContent(t *testing.T) {
	store := setupScriptStoreWithVersions(t)

	row, err := store.Get(context.Background(), "default", "gcenfe02-xml", "1")
	require.NoError(t, err)

	var spec v1alpha1.ScriptSpec
	require.NoError(t, json.Unmarshal(row.Spec, &spec))
	require.Equal(t, "print('versao 1')\r\n", spec.Content)
}

func TestScriptStore_ListTagsReturnsVersionsAndLatest(t *testing.T) {
	store := setupScriptStoreWithVersions(t)

	rows, err := store.ListTags(context.Background(), "default", "gcenfe02-xml")
	require.NoError(t, err)

	tags := make([]string, 0, len(rows))
	for _, row := range rows {
		tags = append(tags, row.Metadata.Tag)
	}
	require.ElementsMatch(t, []string{"1", "2", "latest"}, tags)
}

func TestScriptStore_ListLatestOnlyReturnsOnlyLatestTag(t *testing.T) {
	store := setupScriptStoreWithVersions(t)

	rows, _, err := store.List(context.Background(), v1alpha1store.ListOpts{LatestOnly: true})
	require.NoError(t, err)

	got := make(map[string]string, len(rows))
	for _, row := range rows {
		got[row.Metadata.Name] = row.Metadata.Tag
	}
	require.Equal(t, map[string]string{"gcenfe02-xml": "latest", "outro-script": "latest"}, got)
	require.Len(t, rows, 2)
}
