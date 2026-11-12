package v1alpha1

type LocalApp struct {
	TypeMeta `json:",inline" yaml:",inline"`
	Metadata ObjectMeta     `json:"metadata" yaml:"metadata"`
	Spec     LocalAppSpec   `json:"spec" yaml:"spec"`
	Status   LocalAppStatus `json:"status,omitzero" yaml:"status,omitempty"`
}

func init() {
	MustRegisterKind[*LocalApp, LocalAppSpec](KindLocalApp)
}

type LocalAppStatus struct {
	Status `json:",inline" yaml:",inline"`

	ResolvedSource *LocalAppResolvedSource `json:"resolvedSource,omitempty" yaml:"resolvedSource,omitempty"`
}

type LocalAppResolvedSource struct {
	Artifact *LocalAppResolvedArtifact `json:"artifact,omitempty" yaml:"artifact,omitempty"`
}

type LocalAppResolvedArtifact struct {
	Digest    string `json:"digest,omitempty" yaml:"digest,omitempty"`
	MediaType string `json:"mediaType,omitempty" yaml:"mediaType,omitempty"`
	Size      int64  `json:"size,omitempty" yaml:"size,omitempty"`
}

type LocalAppSpec struct {
	Title          string            `json:"title,omitempty" yaml:"title,omitempty"`
	Description    string            `json:"description" yaml:"description"`
	Usage          string            `json:"usage" yaml:"usage"`
	Requirements   []string          `json:"requirements,omitempty" yaml:"requirements,omitempty"`
	Effects        []string          `json:"effects" yaml:"effects"`
	Area           string            `json:"area" yaml:"area"`
	Tags           []string          `json:"tags,omitempty" yaml:"tags,omitempty"`
	Wheel          string            `json:"wheel" yaml:"wheel"`
	PythonRequires string            `json:"pythonRequires" yaml:"pythonRequires"`
	Commands       LocalAppCommands  `json:"commands" yaml:"commands"`
	Settings       []LocalAppSetting `json:"settings,omitempty" yaml:"settings,omitempty"`
}

type LocalAppCommands struct {
	Start  string `json:"start" yaml:"start"`
	Verify string `json:"verify" yaml:"verify"`
}

type LocalAppSetting struct {
	Name        string `json:"name" yaml:"name"`
	Description string `json:"description,omitempty" yaml:"description,omitempty"`
	Env         string `json:"env,omitempty" yaml:"env,omitempty"`
	File        string `json:"file,omitempty" yaml:"file,omitempty"`
	Key         string `json:"key,omitempty" yaml:"key,omitempty"`
	Required    bool   `json:"required,omitempty" yaml:"required,omitempty"`
	Secret      bool   `json:"secret,omitempty" yaml:"secret,omitempty"`
}
