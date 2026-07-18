#!/bin/sh
# Startup script for the streaming Lambda (Handler = "run.sh"), invoked by the
# AWS Lambda Web Adapter exec wrapper (AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap).
# See terraform/lambda_stream.tf.
exec python3 stream_server.py
