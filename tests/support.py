"""Offline fixture adapters for the chat API and the async matching engine."""

import asyncio

import httpx

from app.models import CompanyInput, Document, MatchRequest
from app.service import match_company_async


def match_company(*args, **kwargs):
    return asyncio.run(match_company_async(*args, **kwargs))


def read_company_directory(directory, name):
    return MatchRequest(company=CompanyInput(name=name), documents=[
        Document(document_id=f"doc_{index:03d}", filename=path.relative_to(directory).as_posix(),
                 content=path.read_bytes().decode("utf-8-sig"))
        for index, path in enumerate(sorted(directory.rglob("*.md")), 1)
    ])


def post_match(client, json):
    created = client.post("/api/v1/sessions", json=json)
    if created.status_code != 201:
        return created
    session = created.json()
    response = client.post(f"/api/v1/sessions/{session['id']}/match", json={"expected_version": session["version"]})
    if response.status_code != 200:
        return response
    return httpx.Response(200, json=response.json()["run"]["result"], headers=response.headers, request=response.request)
