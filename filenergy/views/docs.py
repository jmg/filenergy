"""Self-documenting API: OpenAPI spec + Swagger UI."""
from flask import Blueprint, jsonify, render_template

docs_bp = Blueprint("docs", __name__)


_SPEC: dict = {
    "openapi": "3.0.3",
    "info": {
        "title": "Filenergy API",
        "version": "1.0.0",
        "description": (
            "Programmatic access to your Filenergy workspace. Authenticate "
            "with `Authorization: Bearer <token>` (mint tokens at "
            "`/settings/keys`)."
        ),
    },
    "servers": [{"url": "/api/v1"}],
    "components": {
        "securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"},
        },
        "schemas": {
            "Source": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "score": {"type": "number"},
                },
            },
            "Answer": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "integer"},
                    "message_id": {"type": "integer"},
                    "answer": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Source"},
                    },
                },
            },
            "FileEntry": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                    "indexed": {"type": "boolean"},
                    "status": {"type": "string"},
                    "created_at": {"type": "string", "format": "date-time"},
                },
            },
            "Error": {
                "type": "object",
                "properties": {
                    "error": {"type": "string"},
                    "kind": {"type": "string"},
                },
            },
        },
    },
    "security": [{"bearer": []}],
    "paths": {
        "/health": {
            "get": {
                "summary": "Liveness probe (no auth)",
                "security": [],
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/files": {
            "get": {
                "summary": "List files in your workspace",
                "parameters": [{
                    "in": "query", "name": "limit",
                    "schema": {"type": "integer", "default": 100},
                }],
                "responses": {
                    "200": {
                        "description": "Files",
                        "content": {"application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "files": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/FileEntry"},
                                    }
                                },
                            }
                        }},
                    },
                    "401": {"description": "Invalid API key"},
                },
            },
            "post": {
                "summary": "Upload a file (multipart, field name `files[]`)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "files[]": {"type": "string", "format": "binary"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Uploaded"},
                    "402": {"description": "Plan quota exceeded",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/Error"},
                            }}},
                },
            },
        },
        "/ask": {
            "post": {
                "summary": "Ask a question against your workspace",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["question"],
                                "properties": {
                                    "question": {"type": "string"},
                                    "conversation_id": {"type": "integer"},
                                    "collection_id": {"type": "integer"},
                                    "file_id": {"type": "integer"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Answer",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Answer"},
                        }},
                    },
                    "402": {"description": "Plan quota exceeded"},
                    "503": {"description": "Chat not configured"},
                },
            }
        },
    },
}


@docs_bp.route("/openapi.json")
def openapi_json():
    return jsonify(_SPEC)


@docs_bp.route("/docs")
def docs():
    return render_template("api/docs.html")
