package v1alpha1

import (
	"fmt"
	"regexp"
	"strings"
)

// hookEventNameRe constrains event names to the CamelCase identifiers harness
// settings files use (PreToolUse, SessionStart, ...). Membership in a fixed
// list is deliberately not enforced: harnesses add events over time and a
// registry that rejects a new one would be a publishing outage.
var hookEventNameRe = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9]{0,63}$`)

// hookEntryTypes are the handler kinds a hook entry can declare, with the
// spec field each one requires.
var hookEntryTypes = map[string]string{
	"command":  "command",
	"prompt":   "prompt",
	"agent":    "prompt",
	"http":     "url",
	"mcp_tool": "server+tool",
}

func (h *Hook) Validate() error {
	var errs FieldErrors
	errs = append(errs, ValidateObjectMeta(h.Metadata)...)
	errs = append(errs, validateHookSpec(&h.Spec)...)
	if len(errs) == 0 {
		return nil
	}
	return errs
}

func validateHookSpec(s *HookSpec) FieldErrors {
	var errs FieldErrors
	errs.Append("spec.title", validateTitle(s.Title))
	if s.Language != "" && s.Language != "pt-BR" {
		errs.Append("spec.language", fmt.Errorf("%w: Hooks da Store devem usar pt-BR", ErrInvalidFormat))
	}
	if len(s.Events) == 0 {
		errs.Append("spec.events", fmt.Errorf("%w: informe ao menos um evento", ErrInvalidFormat))
		return errs
	}
	for event, groups := range s.Events {
		path := "spec.events." + event
		if !hookEventNameRe.MatchString(event) {
			errs.Append(path, fmt.Errorf("%w: nome de evento inválido", ErrInvalidFormat))
			continue
		}
		if len(groups) == 0 {
			errs.Append(path, fmt.Errorf("%w: evento sem grupos de matcher", ErrInvalidFormat))
			continue
		}
		for i, group := range groups {
			errs = append(errs, validateHookMatcherGroup(fmt.Sprintf("%s[%d]", path, i), group)...)
		}
	}
	return errs
}

func validateHookMatcherGroup(path string, group HookMatcherGroup) FieldErrors {
	var errs FieldErrors
	if len(group.Hooks) == 0 {
		errs.Append(path+".hooks", fmt.Errorf("%w: grupo sem entradas", ErrInvalidFormat))
		return errs
	}
	for i, entry := range group.Hooks {
		errs = append(errs, validateHookEntry(fmt.Sprintf("%s.hooks[%d]", path, i), entry)...)
	}
	return errs
}

func validateHookEntry(path string, entry HookEntry) FieldErrors {
	var errs FieldErrors
	entryType := strings.TrimSpace(entry.Type)
	required, ok := hookEntryTypes[entryType]
	if !ok {
		errs.Append(path+".type", fmt.Errorf("%w: tipo deve ser command|prompt|agent|http|mcp_tool", ErrInvalidFormat))
		return errs
	}
	switch required {
	case "command":
		if strings.TrimSpace(entry.Command) == "" {
			errs.Append(path+".command", fmt.Errorf("%w: obrigatório para type=command", ErrInvalidFormat))
		}
	case "prompt":
		if strings.TrimSpace(entry.Prompt) == "" {
			errs.Append(path+".prompt", fmt.Errorf("%w: obrigatório para type=%s", ErrInvalidFormat, entryType))
		}
	case "url":
		if strings.TrimSpace(entry.URL) == "" {
			errs.Append(path+".url", fmt.Errorf("%w: obrigatório para type=http", ErrInvalidFormat))
		}
	case "server+tool":
		if strings.TrimSpace(entry.Server) == "" {
			errs.Append(path+".server", fmt.Errorf("%w: obrigatório para type=mcp_tool", ErrInvalidFormat))
		}
		if strings.TrimSpace(entry.Tool) == "" {
			errs.Append(path+".tool", fmt.Errorf("%w: obrigatório para type=mcp_tool", ErrInvalidFormat))
		}
	}
	if entry.Timeout != nil && *entry.Timeout < 0 {
		errs.Append(path+".timeout", fmt.Errorf("%w: timeout não pode ser negativo", ErrInvalidFormat))
	}
	return errs
}
