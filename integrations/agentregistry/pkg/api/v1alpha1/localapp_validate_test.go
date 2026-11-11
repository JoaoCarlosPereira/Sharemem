package v1alpha1

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
)

func validLocalApp() *LocalApp {
	return &LocalApp{
		TypeMeta: TypeMeta{APIVersion: GroupVersion, Kind: KindLocalApp},
		Metadata: ObjectMeta{Namespace: "default", Name: "horas-app", Tag: "1"},
		Spec: LocalAppSpec{
			Title:          "Horas",
			Description:    "Lança as horas da semana",
			Usage:          "horas-app lancar",
			Effects:        []string{"escrita-externa"},
			Area:           "gestao",
			Wheel:          "horas_app-1.0.0-py3-none-any.whl",
			PythonRequires: ">=3.11",
			Commands:       LocalAppCommands{Start: "horas-app", Verify: "horas-app verificar"},
			Settings: []LocalAppSetting{
				{Name: "Token", Description: "Token da API", Env: "HORAS_TOKEN", Required: true, Secret: true},
			},
		},
	}
}

func fullLocalApp() *LocalApp {
	return &LocalApp{
		TypeMeta: TypeMeta{APIVersion: GroupVersion, Kind: KindLocalApp},
		Metadata: ObjectMeta{Namespace: "default", Name: "horas-app", Tag: "1"},
		Spec: LocalAppSpec{
			Title:          "Horas",
			Description:    "Lança as horas da semana",
			Usage:          "horas-app lancar",
			Requirements:   []string{"Conta no Redmine"},
			Effects:        []string{"escrita-externa", "rede"},
			Area:           "gestao",
			Tags:           []string{"horas", "redmine"},
			Wheel:          "horas_app-1.0.0-py3-none-any.whl",
			PythonRequires: ">=3.11",
			Commands:       LocalAppCommands{Start: "horas-app", Verify: "horas-app verificar"},
			Settings: []LocalAppSetting{
				{Name: "Token", Description: "Token da API", Env: "HORAS_TOKEN", File: ".horas/config.json", Key: "token", Required: true, Secret: true},
				{Name: "Projeto", Env: "HORAS_PROJETO"},
			},
		},
	}
}

func requireFullLocalAppSpec(t *testing.T, got LocalAppSpec) {
	t.Helper()
	want := fullLocalApp().Spec
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("spec = %+v, want %+v", got, want)
	}
	setting := got.Settings[0]
	if setting.Name != "Token" || setting.Description != "Token da API" || setting.Env != "HORAS_TOKEN" ||
		setting.File != ".horas/config.json" || setting.Key != "token" || !setting.Required || !setting.Secret {
		t.Fatalf("settings[0] = %+v, want every field filled", setting)
	}
}

func requireRequiredFieldError(t *testing.T, err error, path string) {
	t.Helper()
	var fieldErrs FieldErrors
	if !errors.As(err, &fieldErrs) {
		t.Fatalf("Validate() = %v (%T), want FieldErrors", err, err)
	}
	for _, fieldErr := range fieldErrs {
		if fieldErr.Path == path && errors.Is(fieldErr, ErrRequiredField) {
			return
		}
	}
	t.Fatalf("Validate() = %v, want ErrRequiredField on path %q", err, path)
}

func TestLocalAppValidate_AcceptsValidLocalApp(t *testing.T) {
	if err := validLocalApp().Validate(); err != nil {
		t.Fatalf("Validate() = %v, want nil", err)
	}
}

func TestLocalAppValidate_RejectsInvalidMetadataName(t *testing.T) {
	app := validLocalApp()
	app.Metadata.Name = "Nome_Invalido"

	err := app.Validate()

	if err == nil || !strings.Contains(err.Error(), "metadata.name") {
		t.Fatalf("Validate() = %v, want error on metadata.name", err)
	}
}

func TestLocalAppValidate_RejectsEmptyWheel(t *testing.T) {
	app := validLocalApp()
	app.Spec.Wheel = ""

	requireRequiredFieldError(t, app.Validate(), "spec.wheel")
}

func TestLocalAppValidate_RejectsEmptyStartCommand(t *testing.T) {
	app := validLocalApp()
	app.Spec.Commands.Start = ""

	requireRequiredFieldError(t, app.Validate(), "spec.commands.start")
}

func TestLocalAppValidate_RejectsEmptyVerifyCommand(t *testing.T) {
	app := validLocalApp()
	app.Spec.Commands.Verify = ""

	requireRequiredFieldError(t, app.Validate(), "spec.commands.verify")
}

func TestLocalAppValidate_RejectsBlankRequiredFields(t *testing.T) {
	tests := []struct {
		path   string
		mutate func(*LocalApp)
	}{
		{"spec.wheel", func(a *LocalApp) { a.Spec.Wheel = "   " }},
		{"spec.commands.start", func(a *LocalApp) { a.Spec.Commands.Start = "   " }},
		{"spec.commands.verify", func(a *LocalApp) { a.Spec.Commands.Verify = "   " }},
	}
	for _, tt := range tests {
		t.Run(tt.path, func(t *testing.T) {
			app := validLocalApp()
			tt.mutate(app)

			requireRequiredFieldError(t, app.Validate(), tt.path)
		})
	}
}

func TestLocalAppValidate_ReportsEveryMissingField(t *testing.T) {
	app := validLocalApp()
	app.Spec.Wheel = ""
	app.Spec.Commands = LocalAppCommands{}

	err := app.Validate()

	requireRequiredFieldError(t, err, "spec.wheel")
	requireRequiredFieldError(t, err, "spec.commands.start")
	requireRequiredFieldError(t, err, "spec.commands.verify")
}

func TestKindLocalApp_IsTheLiteralKindName(t *testing.T) {
	if KindLocalApp != "LocalApp" {
		t.Fatalf("KindLocalApp = %q, want %q", KindLocalApp, "LocalApp")
	}
}

func TestLocalApp_JSONRoundTripKeepsEveryField(t *testing.T) {
	data, err := json.Marshal(fullLocalApp())
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	for _, fragment := range []string{
		`"wheel":"horas_app-1.0.0-py3-none-any.whl"`,
		`"pythonRequires":"`,
		`"commands":{"start":"horas-app","verify":"horas-app verificar"}`,
		`"secret":true`,
	} {
		if !strings.Contains(string(data), fragment) {
			t.Fatalf("json = %s, want %s", data, fragment)
		}
	}

	var decoded LocalApp
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("Unmarshal: %v", err)
	}

	requireFullLocalAppSpec(t, decoded.Spec)
}

func TestLocalApp_AccessorsRoundTripThroughObject(t *testing.T) {
	var obj Object = &LocalApp{}
	obj.SetTypeMeta(TypeMeta{APIVersion: GroupVersion, Kind: KindLocalApp})
	obj.SetMetadata(ObjectMeta{Namespace: "default", Name: "horas-app", Tag: "1"})
	meta := obj.GetMetadata()
	if meta == nil || meta.Namespace != "default" || meta.Name != "horas-app" || meta.Tag != "1" {
		t.Fatalf("GetMetadata() = %+v, want default/horas-app:1", meta)
	}
	if obj.GetKind() != "LocalApp" || obj.GetAPIVersion() != GroupVersion {
		t.Fatalf("kind/apiVersion = %q/%q", obj.GetKind(), obj.GetAPIVersion())
	}

	source := fullLocalApp()
	rawSpec, err := source.MarshalSpec()
	if err != nil {
		t.Fatalf("MarshalSpec: %v", err)
	}
	if err := obj.UnmarshalSpec(rawSpec); err != nil {
		t.Fatalf("UnmarshalSpec: %v", err)
	}
	requireFullLocalAppSpec(t, obj.(*LocalApp).Spec)

	source.Status.ObservedGeneration = 3
	source.Status.SetCondition(Condition{Type: "Ready", Status: ConditionTrue, Reason: "Resolved"})
	rawStatus, err := source.MarshalStatus()
	if err != nil {
		t.Fatalf("MarshalStatus: %v", err)
	}
	if err := obj.UnmarshalStatus(rawStatus); err != nil {
		t.Fatalf("UnmarshalStatus: %v", err)
	}
	status := obj.(*LocalApp).Status
	if status.ObservedGeneration != 3 || !status.IsConditionTrue("Ready") {
		t.Fatalf("status = %+v, want observedGeneration 3 and Ready condition", status)
	}
}

func TestScheme_DecodeLocalAppYAML(t *testing.T) {
	doc := []byte(`apiVersion: ar.dev/v1alpha1
kind: LocalApp
metadata:
  name: horas-app
spec:
  wheel: horas_app-1.0.0-py3-none-any.whl
  pythonRequires: ">=3.11"
  commands:
    start: horas-app
    verify: horas-app verificar
  settings:
    - name: Token
      env: HORAS_TOKEN
      required: true
      secret: true
`)

	obj, err := Default.Decode(doc)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}

	app, ok := obj.(*LocalApp)
	if !ok {
		t.Fatalf("Decode: want *LocalApp, got %T", obj)
	}
	if app.GetKind() != KindLocalApp || app.GetKind() != "LocalApp" || app.Kind != "LocalApp" {
		t.Fatalf("kind = %q/%q, want LocalApp", app.GetKind(), app.Kind)
	}
	if app.Spec.Wheel != "horas_app-1.0.0-py3-none-any.whl" || app.Spec.PythonRequires != ">=3.11" {
		t.Fatalf("spec = %+v, want wheel and pythonRequires decoded", app.Spec)
	}
	if app.Spec.Commands.Start != "horas-app" || app.Spec.Commands.Verify != "horas-app verificar" {
		t.Fatalf("commands = %+v, want start and verify decoded", app.Spec.Commands)
	}
	if len(app.Spec.Settings) != 1 || app.Spec.Settings[0].Env != "HORAS_TOKEN" || !app.Spec.Settings[0].Required || !app.Spec.Settings[0].Secret {
		t.Fatalf("settings = %+v, want one required secret setting", app.Spec.Settings)
	}
}

func TestScheme_DecodeLocalAppYAMLWithEveryField(t *testing.T) {
	doc := []byte(`apiVersion: ar.dev/v1alpha1
kind: LocalApp
metadata:
  name: horas-app
spec:
  title: Horas
  description: Lança as horas da semana
  usage: horas-app lancar
  requirements:
    - Conta no Redmine
  effects:
    - escrita-externa
    - rede
  area: gestao
  tags:
    - horas
    - redmine
  wheel: horas_app-1.0.0-py3-none-any.whl
  pythonRequires: ">=3.11"
  commands:
    start: horas-app
    verify: horas-app verificar
  settings:
    - name: Token
      description: Token da API
      env: HORAS_TOKEN
      file: .horas/config.json
      key: token
      required: true
      secret: true
    - name: Projeto
      env: HORAS_PROJETO
`)

	obj, err := Default.Decode(doc)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}

	app, ok := obj.(*LocalApp)
	if !ok {
		t.Fatalf("Decode: want *LocalApp, got %T", obj)
	}
	requireFullLocalAppSpec(t, app.Spec)
}

func TestScheme_DecodeLocalAppJSON(t *testing.T) {
	doc := []byte(`{"apiVersion":"ar.dev/v1alpha1","kind":"LocalApp","metadata":{"name":"horas-app"},"spec":{"wheel":"horas_app-1.0.0-py3-none-any.whl","commands":{"start":"horas-app","verify":"horas-app verificar"}}}`)

	obj, err := Default.Decode(doc)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}

	app, ok := obj.(*LocalApp)
	if !ok {
		t.Fatalf("Decode: want *LocalApp, got %T", obj)
	}
	if app.GetKind() != KindLocalApp || app.GetKind() != "LocalApp" || app.Kind != "LocalApp" {
		t.Fatalf("kind = %q/%q, want LocalApp", app.GetKind(), app.Kind)
	}
	if app.Spec.Wheel != "horas_app-1.0.0-py3-none-any.whl" || app.Spec.Commands.Verify != "horas-app verificar" {
		t.Fatalf("spec = %+v, want wheel and commands decoded", app.Spec)
	}
}
