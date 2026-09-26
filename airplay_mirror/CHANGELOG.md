# Changelog

## 0.1.2

- Pass the phone's volume changes straight through to the speakers instead of baking them into the audio, so they take effect immediately rather than after the buffer has played out. Group speaker levels now act as each speaker's level at 100%.

## 0.1.1

- Fix the add-on staying in "starting" forever (the ingress panel never opened): report a real container health check instead of inheriting OwnTone's.
- Log OwnTone readiness, the speakers it can see, and a hint when no groups exist yet.
- Fix the group editor losing ticked speakers on every status refresh.

## 0.1.0

- Initial release.
