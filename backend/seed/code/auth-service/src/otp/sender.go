// Sends one-time sign-in codes by SMS. EU numbers use the shared EU provider pool (release 2026.10.01-1).
package otp

const (
	euPool        = "shared-eu"
	maxRetries    = 2
	retryBackoffS = 3
)

func Send(region string, phone string, code string) error {
	pool := poolFor(region)
	for attempt := 0; attempt <= maxRetries; attempt++ {
		status, err := provider(pool).Send(phone, "Your sign-in code is "+code)
		if err == nil && status < 300 {
			return nil
		}
		logf("[otp-sender] provider status=%d pool=%s region=%s attempt=%d", status, pool, region, attempt)
		sleep(retryBackoffS)
	}
	return ErrNotSent
}
