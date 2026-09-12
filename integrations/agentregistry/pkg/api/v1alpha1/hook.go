package v1alpha1

// Hook is the typed envelope for kind=Hook resources.
type Hook struct {
	TypeMeta `json:",inline" yaml:",inline"`
	Metadata ObjectMeta `json:"metadata" yaml:"metadata"`
	Spec     HookSpec   `json:"spec" yaml:"spec"`
	Status   HookStatus `json:"status,omitzero" yaml:"status,omitempty"`
}

func init() {
	MustRegisterKind[*Hook, HookSpec](KindHook)
}

// HookSpec is the hook resource's declarative body.
//
// Events carries the same event -> matcher-group shape a harness settings file
// uses, reusing the HookMatcherGroup/HookEntry types the plugin manifest
// already models. A Hook is the standalone form of what a Plugin bundles: one
// publishable, versioned unit a host merges into its settings file.
//
// Hooks that ship executable files (scripts run by a command entry) associate
// them through the artifact subresource, exactly like a complete Skill
// package; commands then reference them under the extraction directory.
type HookSpec struct {
	Title       string `json:"title,omitempty" yaml:"title,omitempty"`
	Description string `json:"description,omitempty" yaml:"description,omitempty"`
	Language    string `json:"language,omitempty" yaml:"language,omitempty"`

	// Events maps a lifecycle event name (PreToolUse, PostToolUse,
	// SessionStart, UserPromptSubmit, ...) to its matcher groups.
	Events map[string][]HookMatcherGroup `json:"events,omitempty" yaml:"events,omitempty"`
}

// HookStatus is the Hook observed-state subresource. It mirrors SkillStatus:
// the embedded Status (conditions + observedGeneration) plus the immutable pin
// of the package associated with this tag.
type HookStatus struct {
	Status `json:",inline" yaml:",inline"`

	ResolvedSource *HookResolvedSource `json:"resolvedSource,omitempty" yaml:"resolvedSource,omitempty"`
}

// HookResolvedSource records the package digest selected for a Hook tag.
type HookResolvedSource struct {
	Artifact *HookResolvedArtifact `json:"artifact,omitempty" yaml:"artifact,omitempty"`
}

// HookResolvedArtifact records the validated package associated with a Hook.
type HookResolvedArtifact struct {
	Digest    string `json:"digest,omitempty" yaml:"digest,omitempty"`
	MediaType string `json:"mediaType,omitempty" yaml:"mediaType,omitempty"`
	Size      int64  `json:"size,omitempty" yaml:"size,omitempty"`
}
