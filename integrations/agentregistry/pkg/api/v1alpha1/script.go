package v1alpha1

type Script struct {
	TypeMeta `json:",inline" yaml:",inline"`
	Metadata ObjectMeta `json:"metadata" yaml:"metadata"`
	Spec     ScriptSpec `json:"spec" yaml:"spec"`
	Status   Status     `json:"status,omitzero" yaml:"status,omitempty"`
}

func init() {
	MustRegisterKind[*Script, ScriptSpec](KindScript)
}

type ScriptSpec struct {
	Title        string   `json:"title,omitempty" yaml:"title,omitempty"`
	Description  string   `json:"description" yaml:"description"`
	Usage        string   `json:"usage" yaml:"usage"`
	FileName     string   `json:"fileName" yaml:"fileName"`
	Language     string   `json:"language" yaml:"language"`
	Requirements []string `json:"requirements,omitempty" yaml:"requirements,omitempty"`
	Effects      []string `json:"effects" yaml:"effects"`
	Area         string   `json:"area" yaml:"area"`
	Tags         []string `json:"tags,omitempty" yaml:"tags,omitempty"`
	Content      string   `json:"content" yaml:"content"`
}
