package v1alpha1

import (
	"encoding/json"
	"strings"
	"testing"
)

func validScript() *Script {
	return &Script{
		TypeMeta: TypeMeta{APIVersion: GroupVersion, Kind: KindScript},
		Metadata: ObjectMeta{Namespace: "default", Name: "gcenfe02-xml", Tag: "1"},
		Spec: ScriptSpec{
			Title:       "Decodificador GCENFE02",
			Description: "Decodifica a coluna XML da GCENFE02",
			Usage:       "python gcenfe02_xml.py <arquivo>",
			FileName:    "gcenfe02_xml.py",
			Language:    "python",
			Effects:     []string{"somente-leitura"},
			Area:        "fiscal",
			Content:     "print('ok')\n",
		},
	}
}

func TestScriptValidate_AcceptsValidScript(t *testing.T) {
	if err := validScript().Validate(); err != nil {
		t.Fatalf("Validate() = %v, want nil", err)
	}
}

func TestScriptValidate_RejectsEmptyContent(t *testing.T) {
	script := validScript()
	script.Spec.Content = ""

	err := script.Validate()

	if err == nil || !strings.Contains(err.Error(), "spec.content") {
		t.Fatalf("Validate() = %v, want error on spec.content", err)
	}
}

func TestScriptValidate_RejectsInvalidMetadataName(t *testing.T) {
	script := validScript()
	script.Metadata.Name = "Nome_Invalido"

	err := script.Validate()

	if err == nil || !strings.Contains(err.Error(), "metadata.name") {
		t.Fatalf("Validate() = %v, want error on metadata.name", err)
	}
}

func TestScript_JSONRoundTripKeepsContent(t *testing.T) {
	content := "linha um com acentuação: ç, ã, é\r\nlinha dois\r\n"
	script := validScript()
	script.Spec.Content = content

	data, err := json.Marshal(script)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	if !strings.Contains(string(data), `"fileName":"gcenfe02_xml.py"`) {
		t.Fatalf("json = %s, want spec.fileName", data)
	}
	var decoded Script
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("Unmarshal: %v", err)
	}

	if decoded.Spec.Content != content {
		t.Fatalf("content = %q, want %q", decoded.Spec.Content, content)
	}
}

func TestScheme_DecodeScript(t *testing.T) {
	doc := []byte(`apiVersion: ar.dev/v1alpha1
kind: Script
metadata:
  name: gcenfe02-xml
spec:
  fileName: gcenfe02_xml.py
  content: "print('ok')"
`)

	obj, err := Default.Decode(doc)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}

	script, ok := obj.(*Script)
	if !ok {
		t.Fatalf("Decode: want *Script, got %T", obj)
	}
	if script.Spec.FileName != "gcenfe02_xml.py" || script.Spec.Content != "print('ok')" {
		t.Fatalf("spec = %+v, want fileName and content decoded", script.Spec)
	}
}
