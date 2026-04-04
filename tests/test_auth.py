from httpx import AsyncClient


class TestRegister:
    async def test_register_success(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "new@example.com",
                "password": "Password1",
                "name": "New User",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["error"] is None
        assert data["data"]["user_id"] is not None
        assert data["data"]["verification_code"] is not None
        assert len(data["data"]["verification_code"]) == 4

    async def test_register_duplicate_email(self, client: AsyncClient, verified_user):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "test@example.com",
                "password": "Password1",
                "name": "Another",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EMAIL_TAKEN"

    async def test_register_weak_password_no_digit(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "weak@example.com",
                "password": "NoDigitsHere",
                "name": "Weak",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_register_weak_password_too_short(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "short@example.com",
                "password": "Ab1",
                "name": "Short",
            },
        )
        assert response.status_code == 422

    async def test_register_missing_name(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "noname@example.com",
                "password": "Password1",
            },
        )
        assert response.status_code == 422


class TestVerify:
    async def test_verify_success_returns_tokens(self, client: AsyncClient):
        reg = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "verify@example.com",
                "password": "Password1",
                "name": "Verifier",
            },
        )
        code = reg.json()["data"]["verification_code"]

        response = await client.post(
            "/api/v1/auth/verify",
            json={
                "email": "verify@example.com",
                "code": code,
            },
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["access_token"] is not None
        assert data["refresh_token"] is not None
        assert data["token_type"] == "bearer"

    async def test_verify_wrong_code(self, client: AsyncClient):
        reg = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "wrongcode@example.com",
                "password": "Password1",
                "name": "Wrong",
            },
        )
        actual_code = reg.json()["data"]["verification_code"]
        # Generate a code that is guaranteed to be wrong
        wrong_code = "0000" if actual_code != "0000" else "1111"

        response = await client.post(
            "/api/v1/auth/verify",
            json={
                "email": "wrongcode@example.com",
                "code": wrong_code,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_CODE"

    async def test_verify_already_verified(self, client: AsyncClient, verified_user):
        response = await client.post(
            "/api/v1/auth/verify",
            json={
                "email": "test@example.com",
                "code": "1234",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ALREADY_VERIFIED"

    async def test_verify_nonexistent_email(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/verify",
            json={
                "email": "nobody@example.com",
                "code": "1234",
            },
        )
        assert response.status_code == 404


class TestResendCode:
    async def test_resend_code_success(self, client: AsyncClient):
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": "resend@example.com",
                "password": "Password1",
                "name": "Resender",
            },
        )
        response = await client.post(
            "/api/v1/auth/resend-code",
            json={
                "email": "resend@example.com",
            },
        )
        # Either 200 (cooldown passed) or 429 (too fast)
        assert response.status_code in (200, 429)

    async def test_resend_code_nonexistent_email(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/resend-code",
            json={
                "email": "nobody@example.com",
            },
        )
        assert response.status_code == 404


class TestLogin:
    async def test_login_success(self, client: AsyncClient, verified_user):
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "test@example.com",
                "password": "Test1234",
            },
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["access_token"] is not None
        assert data["refresh_token"] is not None

    async def test_login_wrong_password(self, client: AsyncClient, verified_user):
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "test@example.com",
                "password": "WrongPass1",
            },
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_login_nonexistent_user(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "nobody@example.com",
                "password": "Password1",
            },
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_login_unverified_user(self, client: AsyncClient):
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": "unverified@example.com",
                "password": "Password1",
                "name": "Unverified",
            },
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "unverified@example.com",
                "password": "Password1",
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "NOT_VERIFIED"


class TestRefresh:
    async def test_refresh_success(self, client: AsyncClient, verified_user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "test@example.com",
                "password": "Test1234",
            },
        )
        old_refresh = login_resp.json()["data"]["refresh_token"]

        response = await client.post(
            "/api/v1/auth/refresh",
            json={
                "refresh_token": old_refresh,
            },
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["access_token"] is not None
        assert data["refresh_token"] is not None
        assert data["refresh_token"] != old_refresh

    async def test_refresh_revoked_token(self, client: AsyncClient, verified_user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "test@example.com",
                "password": "Test1234",
            },
        )
        old_refresh = login_resp.json()["data"]["refresh_token"]

        await client.post(
            "/api/v1/auth/refresh",
            json={
                "refresh_token": old_refresh,
            },
        )

        response = await client.post(
            "/api/v1/auth/refresh",
            json={
                "refresh_token": old_refresh,
            },
        )
        assert response.status_code == 401

    async def test_refresh_invalid_token(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/refresh",
            json={
                "refresh_token": "garbage.token.here",
            },
        )
        assert response.status_code == 401


class TestLogout:
    async def test_logout_success(self, client: AsyncClient, verified_user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "test@example.com",
                "password": "Test1234",
            },
        )
        refresh_token = login_resp.json()["data"]["refresh_token"]

        response = await client.post(
            "/api/v1/auth/logout",
            json={
                "refresh_token": refresh_token,
            },
        )
        assert response.status_code == 200

        refresh_resp = await client.post(
            "/api/v1/auth/refresh",
            json={
                "refresh_token": refresh_token,
            },
        )
        assert refresh_resp.status_code == 401
