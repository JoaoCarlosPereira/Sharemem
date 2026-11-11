//go:build integration

package v1alpha1store_test

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/agentregistry-dev/agentregistry/pkg/api/v1alpha1"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/v1alpha1store"
)

func localAppObj(name, tag, wheel string) *v1alpha1.LocalApp {
	return &v1alpha1.LocalApp{
		TypeMeta: v1alpha1.TypeMeta{APIVersion: v1alpha1.GroupVersion, Kind: v1alpha1.KindLocalApp},
		Metadata: v1alpha1.ObjectMeta{Namespace: "default", Name: name, Tag: tag},
		Spec: v1alpha1.LocalAppSpec{
			Description:    "Lancamento de horas",
			Usage:          "Abrir o aplicativo e lancar as horas",
			Effects:        []string{"grava no Redmine"},
			Area:           "desenvolvimento",
			Wheel:          wheel,
			PythonRequires: ">=3.11",
			Commands:       v1alpha1.LocalAppCommands{Start: "horas-app", Verify: "horas-app verificar"},
		},
	}
}

func setupLocalAppStoreWithVersions(t *testing.T) *v1alpha1store.Store {
	t.Helper()
	pool := v1alpha1store.NewTestPool(t)
	store := v1alpha1store.NewStores(pool, v1alpha1store.TestSchemaRegistry())[v1alpha1.KindLocalApp]
	require.NotNil(t, store)

	ctx := context.Background()
	for _, obj := range []*v1alpha1.LocalApp{
		localAppObj("horas-app", "1", "horas_app-1.0.0-py3-none-any.whl"),
		localAppObj("horas-app", "2", "horas_app-2.0.0-py3-none-any.whl"),
		localAppObj("horas-app", "latest", "horas_app-2.0.0-py3-none-any.whl"),
		localAppObj("outro-app", "1", "outro_app-1.0.0-py3-none-any.whl"),
		localAppObj("outro-app", "latest", "outro_app-1.0.0-py3-none-any.whl"),
	} {
		_, err := store.Upsert(ctx, obj)
		require.NoError(t, err)
	}
	return store
}

func TestLocalAppStore_GetPinnedTagReturnsThatSpec(t *testing.T) {
	store := setupLocalAppStoreWithVersions(t)

	row, err := store.Get(context.Background(), "default", "horas-app", "1")
	require.NoError(t, err)

	var spec v1alpha1.LocalAppSpec
	require.NoError(t, json.Unmarshal(row.Spec, &spec))
	require.Equal(t, localAppObj("horas-app", "1", "horas_app-1.0.0-py3-none-any.whl").Spec, spec)
}

func TestLocalAppStore_ListTagsReturnsVersionsAndLatest(t *testing.T) {
	store := setupLocalAppStoreWithVersions(t)

	rows, err := store.ListTags(context.Background(), "default", "horas-app")
	require.NoError(t, err)

	tags := make([]string, 0, len(rows))
	for _, row := range rows {
		tags = append(tags, row.Metadata.Tag)
	}
	require.ElementsMatch(t, []string{"1", "2", "latest"}, tags)
}

func TestLocalAppStore_ListLatestOnlyReturnsOnlyLatestTag(t *testing.T) {
	store := setupLocalAppStoreWithVersions(t)

	rows, _, err := store.List(context.Background(), v1alpha1store.ListOpts{LatestOnly: true})
	require.NoError(t, err)

	got := make(map[string]string, len(rows))
	for _, row := range rows {
		got[row.Metadata.Name] = row.Metadata.Tag
	}
	require.Equal(t, map[string]string{"horas-app": "latest", "outro-app": "latest"}, got)
	require.Len(t, rows, 2)
}

func TestLocalAppStore_TriggersNotifyRecordEventAndTouchUpdatedAt(t *testing.T) {
	pool := v1alpha1store.NewTestPool(t)
	store := v1alpha1store.NewStores(pool, v1alpha1store.TestSchemaRegistry())[v1alpha1.KindLocalApp]
	events := v1alpha1store.NewControlPlaneEventStore(pool, v1alpha1store.TestSchema())
	ctx := context.Background()

	conn, err := pool.Acquire(ctx)
	require.NoError(t, err)
	defer conn.Release()
	_, err = conn.Exec(ctx, "LISTEN local_apps_status")
	require.NoError(t, err)

	obj := localAppObj("horas-app", "1", "horas_app-1.0.0-py3-none-any.whl")
	_, err = store.Upsert(ctx, obj)
	require.NoError(t, err)

	waitCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	notif, err := conn.Conn().WaitForNotification(waitCtx)
	require.NoError(t, err)
	require.Equal(t, "local_apps_status", notif.Channel)
	var payload struct {
		Op        string `json:"op"`
		Namespace string `json:"namespace"`
		Name      string `json:"name"`
		Tag       string `json:"tag"`
	}
	require.NoError(t, json.Unmarshal([]byte(notif.Payload), &payload))
	require.Equal(t, "INSERT", payload.Op)
	require.Equal(t, "default", payload.Namespace)
	require.Equal(t, "horas-app", payload.Name)
	require.Equal(t, "1", payload.Tag)

	batch, err := events.ListAfter(ctx, 0, 10)
	require.NoError(t, err)
	require.Len(t, batch, 1)
	require.Equal(t, v1alpha1.KindLocalApp, batch[0].Key.Kind)
	require.Equal(t, "horas-app", batch[0].Key.Name)
	require.Equal(t, "1", batch[0].Key.Tag)
	require.Equal(t, "insert", batch[0].Operation)

	before, err := store.Get(ctx, "default", "horas-app", "1")
	require.NoError(t, err)

	obj.Spec.Description = "Lancamento de horas atualizado"
	_, err = store.Upsert(ctx, obj)
	require.NoError(t, err)

	after, err := store.Get(ctx, "default", "horas-app", "1")
	require.NoError(t, err)
	require.True(t, after.Metadata.UpdatedAt.After(before.Metadata.UpdatedAt))

	batch, err = events.ListAfter(ctx, batch[0].Revision, 10)
	require.NoError(t, err)
	require.Len(t, batch, 1)
	require.Equal(t, v1alpha1.KindLocalApp, batch[0].Key.Kind)
	require.Equal(t, "update", batch[0].Operation)
}
