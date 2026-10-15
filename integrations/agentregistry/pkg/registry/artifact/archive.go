package artifact

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"time"
)

const (
	MediaTypeTarGzip       = "application/vnd.agentregistry.skill.v1.tar+gzip"
	HookMediaTypeTarGzip   = "application/vnd.agentregistry.hook.v1.tar+gzip"
	PluginMediaTypeTarGzip = "application/vnd.agentregistry.plugin.v1.tar+gzip"

	// Root files each package kind must carry. A Hook package has the same
	// tar.gz shape as a Skill package but is described by HOOK.md.
	SkillRootFile    = "SKILL.md"
	HookRootFile     = "HOOK.md"
	PluginRootFile   = ".claude-plugin/plugin.json"
	PluginReadmeFile = "README.md"

	MaxArchiveBytes      int64 = 16 << 20
	MaxUncompressedBytes int64 = 32 << 20
	MaxFileBytes         int64 = 4 << 20
	MaxFiles                   = 256
	MaxPathBytes               = 240
)

var ErrInvalidArchive = errors.New("invalid package artifact archive")

type File struct {
	Path string
	Size int64
	Mode int64
}

type ContentFile struct {
	File
	Content []byte
}

func PackDir(dir string) ([]byte, error) {
	files := make(map[string][]byte)
	err := filepath.WalkDir(dir, func(filePath string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if filePath == dir {
			return nil
		}
		rel, err := filepath.Rel(dir, filePath)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if entry.IsDir() {
			if entry.Name() == ".git" {
				return filepath.SkipDir
			}
			return validatePath(rel)
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("%w: symlink %q is not allowed", ErrInvalidArchive, rel)
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("%w: special file %q is not allowed", ErrInvalidArchive, rel)
		}
		if info.Size() > MaxFileBytes {
			return fmt.Errorf("%w: file %q exceeds %d bytes", ErrInvalidArchive, rel, MaxFileBytes)
		}
		data, err := os.ReadFile(filePath)
		if err != nil {
			return err
		}
		files[rel] = data
		return nil
	})
	if err != nil {
		if errors.Is(err, ErrInvalidArchive) {
			return nil, err
		}
		return nil, fmt.Errorf("%w: read source tree: %v", ErrInvalidArchive, err)
	}
	return PackFiles(files)
}

func PackFiles(files map[string][]byte) ([]byte, error) {
	if _, ok := files["SKILL.md"]; !ok {
		return nil, fmt.Errorf("%w: SKILL.md is required at archive root", ErrInvalidArchive)
	}
	if len(files) > MaxFiles {
		return nil, fmt.Errorf("%w: too many files (limit %d)", ErrInvalidArchive, MaxFiles)
	}

	paths := make([]string, 0, len(files))
	var total int64
	for name, data := range files {
		if err := validatePath(name); err != nil {
			return nil, err
		}
		if int64(len(data)) > MaxFileBytes {
			return nil, fmt.Errorf("%w: file %q exceeds %d bytes", ErrInvalidArchive, name, MaxFileBytes)
		}
		total += int64(len(data))
		if total > MaxUncompressedBytes {
			return nil, fmt.Errorf("%w: files exceed %d bytes", ErrInvalidArchive, MaxUncompressedBytes)
		}
		paths = append(paths, name)
	}
	sort.Strings(paths)

	var out bytes.Buffer
	gz := gzip.NewWriter(&out)
	gz.Header.ModTime = time.Unix(0, 0)
	gz.Header.OS = 255
	tw := tar.NewWriter(gz)
	for _, name := range paths {
		data := files[name]
		header := &tar.Header{
			Name:       name,
			Mode:       0o644,
			Size:       int64(len(data)),
			Typeflag:   tar.TypeReg,
			ModTime:    time.Unix(0, 0),
			AccessTime: time.Unix(0, 0),
			ChangeTime: time.Unix(0, 0),
			Format:     tar.FormatPAX,
		}
		if err := tw.WriteHeader(header); err != nil {
			return nil, fmt.Errorf("write tar header %q: %w", name, err)
		}
		if _, err := tw.Write(data); err != nil {
			return nil, fmt.Errorf("write tar file %q: %w", name, err)
		}
	}
	if err := tw.Close(); err != nil {
		return nil, fmt.Errorf("close tar writer: %w", err)
	}
	if err := gz.Close(); err != nil {
		return nil, fmt.Errorf("close gzip writer: %w", err)
	}
	if int64(out.Len()) > MaxArchiveBytes {
		return nil, fmt.Errorf("%w: compressed archive exceeds %d bytes", ErrInvalidArchive, MaxArchiveBytes)
	}
	return out.Bytes(), nil
}

// Validate checks a Skill package (SKILL.md at the root).
func Validate(data []byte) ([]File, error) {
	return ValidatePackage(data, SkillRootFile)
}

// ValidatePackage checks the archive shape shared by every package kind and
// that each of requiredRootFiles is present as a regular file.
func ValidatePackage(data []byte, requiredRootFiles ...string) ([]File, error) {
	if len(data) == 0 {
		return nil, fmt.Errorf("%w: archive is empty", ErrInvalidArchive)
	}
	if int64(len(data)) > MaxArchiveBytes {
		return nil, fmt.Errorf("%w: compressed archive exceeds %d bytes", ErrInvalidArchive, MaxArchiveBytes)
	}
	gz, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("%w: open gzip: %v", ErrInvalidArchive, err)
	}
	gz.Multistream(false)
	defer func() { _ = gz.Close() }()

	tr := tar.NewReader(gz)
	seen := map[string]struct{}{}
	files := make([]File, 0)
	var total int64
	present := map[string]struct{}{}
	for {
		header, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%w: read tar: %v", ErrInvalidArchive, err)
		}
		name := strings.TrimSuffix(header.Name, "/")
		if err := validatePath(name); err != nil {
			return nil, err
		}
		if _, ok := seen[name]; ok {
			return nil, fmt.Errorf("%w: duplicate path %q", ErrInvalidArchive, name)
		}
		seen[name] = struct{}{}

		switch header.Typeflag {
		case tar.TypeDir:
			continue
		case tar.TypeReg, tar.TypeRegA:
		default:
			return nil, fmt.Errorf("%w: archive entry %q has forbidden type %d", ErrInvalidArchive, name, header.Typeflag)
		}
		if header.Size < 0 || header.Size > MaxFileBytes {
			return nil, fmt.Errorf("%w: file %q exceeds %d bytes", ErrInvalidArchive, name, MaxFileBytes)
		}
		if len(files) >= MaxFiles {
			return nil, fmt.Errorf("%w: too many files (limit %d)", ErrInvalidArchive, MaxFiles)
		}
		total += header.Size
		if total > MaxUncompressedBytes {
			return nil, fmt.Errorf("%w: files exceed %d bytes", ErrInvalidArchive, MaxUncompressedBytes)
		}
		if _, err := io.Copy(io.Discard, io.LimitReader(tr, header.Size+1)); err != nil {
			return nil, fmt.Errorf("%w: read file %q: %v", ErrInvalidArchive, name, err)
		}
		files = append(files, File{Path: name, Size: header.Size, Mode: header.Mode})
		present[name] = struct{}{}
	}
	for _, required := range requiredRootFiles {
		if _, ok := present[required]; !ok {
			return nil, fmt.Errorf("%w: %s is required at archive root", ErrInvalidArchive, required)
		}
	}
	slices.SortFunc(files, func(a, b File) int { return strings.Compare(a.Path, b.Path) })
	return files, nil
}

// ReadFiles returns the files of a Skill package.
func ReadFiles(data []byte) ([]ContentFile, error) {
	return ReadPackageFiles(data, SkillRootFile)
}

// ReadPackageFiles returns the files of a package after ValidatePackage.
func ReadPackageFiles(data []byte, requiredRootFiles ...string) ([]ContentFile, error) {
	if _, err := ValidatePackage(data, requiredRootFiles...); err != nil {
		return nil, err
	}
	gz, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("%w: open gzip: %v", ErrInvalidArchive, err)
	}
	defer func() { _ = gz.Close() }()
	tr := tar.NewReader(gz)
	files := make([]ContentFile, 0)
	for {
		header, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%w: read tar: %v", ErrInvalidArchive, err)
		}
		if header.Typeflag == tar.TypeDir {
			continue
		}
		content, err := io.ReadAll(io.LimitReader(tr, header.Size+1))
		if err != nil || int64(len(content)) != header.Size {
			return nil, fmt.Errorf("%w: read file %q", ErrInvalidArchive, header.Name)
		}
		files = append(files, ContentFile{
			File:    File{Path: header.Name, Size: header.Size, Mode: header.Mode},
			Content: content,
		})
	}
	slices.SortFunc(files, func(a, b ContentFile) int { return strings.Compare(a.Path, b.Path) })
	return files, nil
}

func validatePath(name string) error {
	if name == "" || len(name) > MaxPathBytes {
		return fmt.Errorf("%w: invalid path length for %q", ErrInvalidArchive, name)
	}
	if strings.ContainsRune(name, '\\') || path.IsAbs(name) || path.Clean(name) != name {
		return fmt.Errorf("%w: unsafe path %q", ErrInvalidArchive, name)
	}
	if strings.HasPrefix(name, "../") || name == ".." || strings.Contains(name, "/../") {
		return fmt.Errorf("%w: path traversal in %q", ErrInvalidArchive, name)
	}
	return nil
}
