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
  /**
   * Optional: Lambda Function URL for the streaming chat endpoint (POST /chat/stream),
   * e.g. "https://xxxx.lambda-url.us-east-1.on.aws". Requires
   * enable_chat_streaming = true in terraform (see terraform/lambda_stream.tf).
   * Leave blank to always use the buffered POST /chat endpoint.
   */
  streamApiBase: "",
};
