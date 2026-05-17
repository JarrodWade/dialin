/* Copy to dialin-config.js (gitignored) and fill in locally.
 *
 *   cp web/dialin-config.example.js web/dialin-config.js
 *
 * Or run after terraform apply:
 *   make web-config
 */
window.DIALIN_CONFIG = window.DIALIN_CONFIG || {
  apiBase: "",
  clerkPublishableKey: "",
  /** Keep in sync with Lambda env CHAT_HISTORY_TURN_LIMIT (default 24). */
  chatHistoryTurnLimit: 24,
};
