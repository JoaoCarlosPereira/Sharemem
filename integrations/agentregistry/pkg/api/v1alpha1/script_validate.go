package v1alpha1

import "fmt"

func (s *Script) Validate() error {
	var errs FieldErrors
	errs = append(errs, ValidateObjectMeta(s.Metadata)...)
	if s.Spec.Content == "" {
		errs.Append("spec.content", fmt.Errorf("%w", ErrRequiredField))
	}
	if len(errs) == 0 {
		return nil
	}
	return errs
}
