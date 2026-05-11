locals {
  # When set (CLERK_JWT_ISSUER env), Lambda verifies Clerk JWT via JWKS (no Gateway JWT authorizer).
  clerk_auth_enabled = trimspace(var.clerk_jwt_issuer) != ""
}
