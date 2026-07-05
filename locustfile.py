import random
from locust import HttpUser, task, between # type: ignore

# Member 2 seeded 100 users round-robin across three roles:
#   index % 3 == 0 -> web    (webuser_0, webuser_3, ... webuser_99)
#   index % 3 == 1 -> mobile (mobileuser_1, mobileuser_4, ... mobileuser_97)
#   index % 3 == 2 -> api    (apiuser_2, apiuser_5, ... apiuser_98)
#   all passwords are "pass"
WEB_USERS = [(f"webuser_{i}", "pass") for i in range(0, 100, 3)]
MOBILE_USERS = [(f"mobileuser_{i}", "pass") for i in range(1, 100, 3)]
API_USERS = [(f"apiuser_{i}", "pass") for i in range(2, 100, 3)]


class BaseAPIUser(HttpUser):
    abstract = True
    user_pool = []

    def on_start(self):
        username, password = random.choice(self.user_pool)
        response = self.client.post(
            "/login",
            json={"username": username, "password": password},
            name="/login",
        )
        if response.status_code == 200:
            self.token = response.json().get("token") or response.json().get("access_token")
            self.headers = {"Authorization": f"Bearer {self.token}"}
        else:
            self.token = None
            self.headers = {}

    def on_stop(self):
        if self.token:
            self.client.post("/logout", headers=self.headers, name="/logout")


class WebUser(BaseAPIUser):
    weight = 50
    wait_time = between(2, 5)
    user_pool = WEB_USERS

    @task(3)
    def get_me(self):
        if not self.token:
            return
        self.client.get("/me", headers=self.headers, name="/me")

    @task(2)
    def get_api_data(self):
        if not self.token:
            return
        self.client.get("/api/data", headers=self.headers, name="/api/data")

    @task(2)
    def update_profile(self):
        if not self.token:
            return
        self.client.post(
            "/profile/update",
            json={"bio": "web session"},
            headers=self.headers,
            name="/profile/update",
        )

    @task(1)
    def health_check(self):
        self.client.get("/health", name="/health")


class MobileUser(BaseAPIUser):
    weight = 35
    wait_time = between(1, 3)
    user_pool = MOBILE_USERS

    @task(4)
    def get_me(self):
        if not self.token:
            return
        self.client.get("/me", headers=self.headers, name="/me")

    @task(3)
    def get_api_data(self):
        if not self.token:
            return
        self.client.get("/api/data", headers=self.headers, name="/api/data")

    @task(1)
    def update_profile(self):
        if not self.token:
            return
        self.client.post(
            "/profile/update",
            json={"bio": "mobile session"},
            headers=self.headers,
            name="/profile/update",
        )

    @task(1)
    def health_check(self):
        self.client.get("/health", name="/health")


class APIUser(BaseAPIUser):
    weight = 15
    wait_time = between(0.1, 0.5)
    user_pool = API_USERS

    @task(5)
    def get_api_data(self):
        if not self.token:
            return
        self.client.get("/api/data", headers=self.headers, name="/api/data")

    @task(2)
    def get_me(self):
        if not self.token:
            return
        self.client.get("/me", headers=self.headers, name="/me")

    @task(1)
    def health_check(self):
        self.client.get("/health", name="/health")
