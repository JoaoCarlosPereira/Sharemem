package v1alpha1

import (
	"fmt"
	"strings"
)

func (a *LocalApp) Validate() error {
	var errs FieldErrors
	errs = append(errs, ValidateObjectMeta(a.Metadata)...)
	if strings.TrimSpace(a.Spec.Wheel) == "" {
		errs.Append("spec.wheel", fmt.Errorf("%w", ErrRequiredField))
	}
	if strings.TrimSpace(a.Spec.Commands.Start) == "" {
		errs.Append("spec.commands.start", fmt.Errorf("%w", ErrRequiredField))
	}
	if strings.TrimSpace(a.Spec.Commands.Verify) == "" {
		errs.Append("spec.commands.verify", fmt.Errorf("%w", ErrRequiredField))
	}
	if len(errs) == 0 {
		return nil
	}
	return errs
}
