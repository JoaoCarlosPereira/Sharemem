// Package skillartifact owns the package artifact upload/download subresource
// of the kinds that ship a complete directory: Skill and Hook. Register is
// called once per kind; Config carries the kind's identity (route plural,
// media type, authorization Kind) so the handlers stay shared.
package skillartifact

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"mime"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/danielgtaylor/huma/v2"

	"github.com/agentregistry-dev/agentregistry/pkg/api/v1alpha1"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/artifact"
	pkgdb "github.com/agentregistry-dev/agentregistry/pkg/registry/database"
	"github.com/agentregistry-dev/agentregistry/pkg/registry/resource"
	"github.com/agentregistry-dev/agentregistry/pkg/types"
)

const (
	MediaType = "application/vnd.agentregistry.skill.v1.tar+gzip"
	// HookMediaType is the Hook package media type. Hook packages are the
	// same validated tar.gz shape as Skill packages; only the vendor media
	// type differs so a client cannot cross-post one kind's bytes to the
	// other's route.
	HookMediaType = "application/vnd.agentregistry.hook.v1.tar+gzip"
)

type Config struct {
	BasePrefix string
	Store      types.SkillArtifactStore
	Authorize  func(context.Context, resource.AuthorizeInput) error

	// Kind, Plural, MediaType and Label identify the kind these routes serve.
	// Zero values default to Skill, keeping existing call sites unchanged.
	Kind      string
	Plural    string
	MediaType string
	Label     string
}

func (c Config) withDefaults() Config {
	if c.Kind == "" {
		c.Kind = v1alpha1.KindSkill
	}
	if c.Plural == "" {
		c.Plural = "skills"
	}
	if c.MediaType == "" {
		c.MediaType = MediaType
	}
	if c.Label == "" {
		c.Label = c.Kind
	}
	return c
}

type artifactInput struct {
	Namespace string `query:"namespace" doc:"Namespace (defaults to 'default')."`
	Name      string `path:"name"`
	Tag       string `path:"tag"`
}

type putArtifactInput struct {
	Namespace string `query:"namespace" doc:"Namespace (defaults to 'default')."`
	Name      string `path:"name"`
	Tag       string `path:"tag"`
	Digest    string `header:"Digest" doc:"Optional RFC 3230 SHA-256 digest (sha-256=<base64>)."`
	RawBody   []byte `contentType:"application/vnd.agentregistry.skill.v1.tar+gzip"`
}

// putHookArtifactInput mirrors putArtifactInput for the Hook media type. The
// raw-body content type is a struct tag Huma reads at registration, so it
// cannot come from Config — one input type per media type is required.
type putHookArtifactInput struct {
	Namespace string `query:"namespace" doc:"Namespace (defaults to 'default')."`
	Name      string `path:"name"`
	Tag       string `path:"tag"`
	Digest    string `header:"Digest" doc:"Optional RFC 3230 SHA-256 digest (sha-256=<base64>)."`
	RawBody   []byte `contentType:"application/vnd.agentregistry.hook.v1.tar+gzip"`
}

type putArtifactOutput struct {
	ETag   string `header:"ETag"`
	Digest string `header:"Digest"`
}

func Register(api huma.API, cfg Config) {
	if cfg.Store == nil {
		return
	}
	cfg = cfg.withDefaults()
	path := strings.TrimRight(cfg.BasePrefix, "/") + "/" + cfg.Plural + "/{name}/{tag}/artifact"
	lower := strings.ToLower(cfg.Label)

	putOperation := huma.Operation{
		OperationID:   "put-" + lower + "-artifact",
		Method:        http.MethodPut,
		Path:          path,
		Summary:       "Upload an immutable " + cfg.Label + " tar.gz artifact",
		Tags:          []string{cfg.Plural},
		DefaultStatus: http.StatusCreated,
	}
	// One registration per media type: the raw-body content type is a struct
	// tag Huma reads off the input type at registration time.
	if cfg.MediaType == HookMediaType {
		huma.Register(api, putOperation,
			func(ctx context.Context, in *putHookArtifactInput) (*putArtifactOutput, error) {
				return putArtifact(ctx, cfg, in.Namespace, in.Name, in.Tag, in.Digest, in.RawBody)
			})
	} else {
		huma.Register(api, putOperation,
			func(ctx context.Context, in *putArtifactInput) (*putArtifactOutput, error) {
				return putArtifact(ctx, cfg, in.Namespace, in.Name, in.Tag, in.Digest, in.RawBody)
			})
	}

	huma.Register(api, huma.Operation{
		OperationID: "get-" + lower + "-artifact",
		Method:      http.MethodGet,
		Path:        path,
		Summary:     "Download a " + cfg.Label + " tar.gz artifact",
		Tags:        []string{cfg.Plural},
	}, func(ctx context.Context, in *artifactInput) (*huma.StreamResponse, error) {
		key, err := resolveKey(in.Namespace, in.Name, in.Tag)
		if err != nil {
			return nil, err
		}
		if cfg.Authorize != nil {
			if err := cfg.Authorize(ctx, resource.AuthorizeInput{
				Verb: "get", Kind: cfg.Kind,
				Namespace: key.Namespace, Name: key.Name, Tag: key.Tag,
			}); err != nil {
				return nil, err
			}
		}
		artifact, err := cfg.Store.Get(ctx, key)
		if err != nil {
			return nil, mapStoreError(err, key, "load")
		}
		if artifact.Content == nil || artifact.Size < 0 || len(artifact.SHA256) != sha256.Size {
			if artifact.Content != nil {
				_ = artifact.Content.Close()
			}
			return nil, huma.Error500InternalServerError("load "+cfg.Label+" artifact", errors.New("artifact store returned invalid metadata"))
		}

		return &huma.StreamResponse{Body: func(hctx huma.Context) {
			defer artifact.Content.Close()
			hctx.SetHeader("Content-Type", cfg.MediaType)
			hctx.SetHeader("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{
				"filename": key.Name + "-" + key.Tag + ".tar.gz",
			}))
			hctx.SetHeader("Content-Length", strconv.FormatInt(artifact.Size, 10))
			hctx.SetHeader("ETag", etag(artifact.SHA256))
			hctx.SetHeader("Digest", digestHeader(artifact.SHA256))
			hctx.SetHeader("X-Content-Type-Options", "nosniff")
			_, _ = io.Copy(hctx.BodyWriter(), artifact.Content)
		}}, nil
	})

	huma.Register(api, huma.Operation{
		OperationID: "download-" + lower + "-package",
		Method:      http.MethodGet,
		Path:        strings.TrimRight(cfg.BasePrefix, "/") + "/" + cfg.Plural + "/{name}/{tag}/download",
		Summary:     "Download a complete " + cfg.Label + " package as ZIP",
		Tags:        []string{cfg.Plural},
	}, func(ctx context.Context, in *artifactInput) (*huma.StreamResponse, error) {
		key, err := resolveKey(in.Namespace, in.Name, in.Tag)
		if err != nil {
			return nil, err
		}
		if cfg.Authorize != nil {
			if err := cfg.Authorize(ctx, resource.AuthorizeInput{
				Verb: "get", Kind: cfg.Kind,
				Namespace: key.Namespace, Name: key.Name, Tag: key.Tag,
			}); err != nil {
				return nil, err
			}
		}
		stored, err := cfg.Store.Get(ctx, key)
		if err != nil {
			return nil, mapStoreError(err, key, "load")
		}
		if stored.Content == nil || stored.Size < 0 {
			if stored.Content != nil {
				_ = stored.Content.Close()
			}
			return nil, huma.Error500InternalServerError("load "+cfg.Label+" package", errors.New("artifact store returned invalid metadata"))
		}
		archiveData, readErr := io.ReadAll(io.LimitReader(stored.Content, 32<<20+1))
		_ = stored.Content.Close()
		if readErr != nil {
			return nil, huma.Error500InternalServerError("read "+cfg.Label+" artifact", readErr)
		}
		files, readErr := artifact.ReadFiles(archiveData)
		if readErr != nil {
			return nil, huma.Error500InternalServerError("validate "+cfg.Label+" artifact", readErr)
		}
		var out bytes.Buffer
		zw := zip.NewWriter(&out)
		for _, file := range files {
			header := &zip.FileHeader{Name: file.Path, Method: zip.Deflate}
			header.SetMode(fs.FileMode(file.Mode))
			writer, writeErr := zw.CreateHeader(header)
			if writeErr != nil {
				return nil, huma.Error500InternalServerError("create "+cfg.Label+" ZIP", writeErr)
			}
			if _, writeErr = writer.Write(file.Content); writeErr != nil {
				return nil, huma.Error500InternalServerError("write "+cfg.Label+" ZIP", writeErr)
			}
		}
		if err := zw.Close(); err != nil {
			return nil, huma.Error500InternalServerError("close "+cfg.Label+" ZIP", err)
		}
		return &huma.StreamResponse{Body: func(hctx huma.Context) {
			hctx.SetHeader("Content-Type", "application/zip")
			hctx.SetHeader("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{
				"filename": key.Name + "-" + key.Tag + ".zip",
			}))
			hctx.SetHeader("Content-Length", strconv.Itoa(out.Len()))
			hctx.SetHeader("X-Content-Type-Options", "nosniff")
			_, _ = hctx.BodyWriter().Write(out.Bytes())
		}}, nil
	})
}

// putArtifact is the media-type-agnostic body of the PUT handler.
func putArtifact(
	ctx context.Context,
	cfg Config,
	namespace, name, tag, digestHeaderValue string,
	body []byte,
) (*putArtifactOutput, error) {
	key, err := resolveKey(namespace, name, tag)
	if err != nil {
		return nil, err
	}
	if cfg.Authorize != nil {
		if err := cfg.Authorize(ctx, resource.AuthorizeInput{
			Verb: "update", Kind: cfg.Kind,
			Namespace: key.Namespace, Name: key.Name, Tag: key.Tag,
		}); err != nil {
			return nil, err
		}
	}
	if len(body) == 0 {
		return nil, huma.Error422UnprocessableEntity("artifact body must not be empty")
	}

	sum := sha256.Sum256(body)
	if err := validateDigest(digestHeaderValue, sum[:]); err != nil {
		return nil, huma.Error422UnprocessableEntity(err.Error())
	}
	stored, err := cfg.Store.Put(ctx, key, bytes.NewReader(body), types.SkillArtifactPutOptions{
		Size:   int64(len(body)),
		SHA256: append([]byte(nil), sum[:]...),
	})
	if err != nil {
		return nil, mapStoreError(err, key, "store")
	}
	digest := stored.SHA256
	if len(digest) == 0 {
		digest = sum[:]
	}
	return &putArtifactOutput{ETag: etag(digest), Digest: digestHeader(digest)}, nil
}

func resolveKey(namespace, rawName, rawTag string) (types.SkillArtifactKey, error) {
	if namespace == "" {
		namespace = v1alpha1.DefaultNamespace
	}
	if rawName == "" || rawTag == "" {
		return types.SkillArtifactKey{}, huma.Error422UnprocessableEntity("package artifact name and tag are required")
	}
	name, err := url.PathUnescape(rawName)
	if err != nil {
		return types.SkillArtifactKey{}, huma.Error422UnprocessableEntity("invalid name path segment: " + err.Error())
	}
	tag, err := url.PathUnescape(rawTag)
	if err != nil {
		return types.SkillArtifactKey{}, huma.Error422UnprocessableEntity("invalid tag path segment: " + err.Error())
	}
	probe := &v1alpha1.Skill{Metadata: v1alpha1.ObjectMeta{Namespace: namespace, Name: name, Tag: tag}, Spec: v1alpha1.SkillSpec{Title: "artifact"}}
	if err := probe.Validate(); err != nil {
		return types.SkillArtifactKey{}, huma.Error422UnprocessableEntity("invalid package artifact identity: " + err.Error())
	}
	return types.SkillArtifactKey{Namespace: namespace, Name: name, Tag: tag}, nil
}

func validateDigest(value string, actual []byte) error {
	if value == "" {
		return nil
	}
	const prefix = "sha-256="
	if !strings.HasPrefix(value, prefix) || strings.Contains(value[len(prefix):], ",") {
		return fmt.Errorf("Digest must use sha-256=<base64>")
	}
	expected, err := base64.StdEncoding.DecodeString(strings.TrimSpace(strings.TrimPrefix(value, prefix)))
	if err != nil || len(expected) != sha256.Size {
		return fmt.Errorf("Digest contains an invalid SHA-256 value")
	}
	if !bytes.Equal(expected, actual) {
		return fmt.Errorf("Digest does not match the uploaded artifact")
	}
	return nil
}

func mapStoreError(err error, key types.SkillArtifactKey, operation string) error {
	switch {
	case errors.Is(err, pkgdb.ErrNotFound):
		return huma.Error404NotFound(fmt.Sprintf("package artifact %q/%q@%q not found", key.Namespace, key.Name, key.Tag))
	case errors.Is(err, pkgdb.ErrAlreadyExists):
		return huma.Error409Conflict(fmt.Sprintf("package artifact %q/%q@%q already exists", key.Namespace, key.Name, key.Tag))
	case errors.Is(err, pkgdb.ErrInvalidInput):
		return huma.Error422UnprocessableEntity("invalid package artifact: " + err.Error())
	default:
		return huma.Error500InternalServerError(operation+" package artifact", err)
	}
}

func digestHeader(digest []byte) string {
	return "sha-256=" + base64.StdEncoding.EncodeToString(digest)
}

func etag(digest []byte) string {
	return `"sha256:` + hex.EncodeToString(digest) + `"`
}
