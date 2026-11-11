//go:build integration

package database

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"io"
	"testing"

	"github.com/agentregistry-dev/agentregistry/pkg/api/v1alpha1"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/artifact"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/v1alpha1store"
)

func TestPostgresArtifactStoreRoundTrip(t *testing.T) {
	pool := v1alpha1store.NewTestPool(t)
	skills := v1alpha1store.NewStore(pool, v1alpha1store.TestSchema(), "skills")
	ctx := context.Background()
	_, err := skills.Upsert(ctx, &v1alpha1.Skill{
		Metadata: v1alpha1.ObjectMeta{Namespace: "default", Name: "demo", Tag: "v1"},
		Spec:     v1alpha1.SkillSpec{Title: "Demo"},
	})
	if err != nil {
		t.Fatal(err)
	}

	archive, err := artifact.PackFiles(map[string][]byte{"SKILL.md": []byte("# Demo"), "references/a.md": []byte("a")})
	if err != nil {
		t.Fatal(err)
	}
	store := NewPostgresArtifactStore(pool, v1alpha1store.TestSchema())
	ref := artifact.SkillRef{Namespace: "default", Name: "demo", Tag: "v1"}
	descriptor, err := store.Put(ctx, ref, archive)
	if err != nil {
		t.Fatal(err)
	}
	if descriptor.Digest == "" || descriptor.Size != int64(len(archive)) {
		t.Fatalf("unexpected descriptor: %+v", descriptor)
	}
	files, err := store.ListFiles(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 2 {
		t.Fatalf("unexpected files: %+v", files)
	}
	gotDescriptor, reader, err := store.Open(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	got, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal(err)
	}
	if gotDescriptor.Digest != descriptor.Digest || string(got) != string(archive) {
		t.Fatal("stored archive did not round-trip")
	}

	if err := skills.Delete(ctx, "default", "demo", "v1"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Get(ctx, ref); err == nil {
		t.Fatal("artifact must cascade-delete with Skill")
	}
}

func TestPostgresLocalAppArtifactStoreRoundTrip(t *testing.T) {
	pool := v1alpha1store.NewTestPool(t)
	apps := v1alpha1store.NewStores(pool, v1alpha1store.TestSchemaRegistry())[v1alpha1.KindLocalApp]
	ctx := context.Background()
	_, err := apps.Upsert(ctx, &v1alpha1.LocalApp{
		Metadata: v1alpha1.ObjectMeta{Namespace: "default", Name: "horas-app", Tag: "1"},
		Spec:     v1alpha1.LocalAppSpec{Description: "Lancamento de horas"},
	})
	if err != nil {
		t.Fatal(err)
	}

	archive := packTarGzip(t, map[string]string{"APP.md": "# horas-app", "horas_app-1.0.0-py3-none-any.whl": "whl"})
	store := NewPostgresLocalAppArtifactStore(pool, v1alpha1store.TestSchema())
	ref := artifact.SkillRef{Namespace: "default", Name: "horas-app", Tag: "1"}
	descriptor, err := store.Put(ctx, ref, archive)
	if err != nil {
		t.Fatal(err)
	}
	if descriptor.MediaType != artifact.LocalAppMediaTypeTarGzip || descriptor.Size != int64(len(archive)) {
		t.Fatalf("unexpected descriptor: %+v", descriptor)
	}

	var linked int
	if err := pool.QueryRow(ctx, "SELECT count(*) FROM "+v1alpha1store.TestSchema().Qualify("localapp_artifacts")+" WHERE namespace='default' AND name='horas-app' AND tag='1' AND digest=$1", descriptor.Digest).Scan(&linked); err != nil {
		t.Fatal(err)
	}
	if linked != 1 {
		t.Fatalf("expected 1 row in localapp_artifacts, got %d", linked)
	}

	gotDescriptor, reader, err := store.Open(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	got, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal(err)
	}
	if gotDescriptor.Digest != descriptor.Digest || gotDescriptor.MediaType != artifact.LocalAppMediaTypeTarGzip || string(got) != string(archive) {
		t.Fatal("stored archive did not round-trip")
	}

	if _, err := store.Put(ctx, artifact.SkillRef{Namespace: "default", Name: "horas-app", Tag: "2"}, archive); err == nil {
		t.Fatal("artifact must require the LocalApp row of the same tag")
	}

	if err := apps.Delete(ctx, "default", "horas-app", "1"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Get(ctx, ref); err == nil {
		t.Fatal("artifact must cascade-delete with LocalApp")
	}
}

func packTarGzip(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var out bytes.Buffer
	gz := gzip.NewWriter(&out)
	tw := tar.NewWriter(gz)
	for name, content := range files {
		if err := tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(content))}); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return out.Bytes()
}
