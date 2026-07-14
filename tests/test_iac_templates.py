import re
import unittest
from pathlib import Path
from urllib.parse import quote

import jinja2
import yaml


ROOT = Path(__file__).parents[1]


def template_context():
    defaults = yaml.safe_load(
        (ROOT / "ansible/roles/frigate_vm/defaults/main.yml").read_text(encoding="utf-8")
    )
    values = yaml.safe_load(
        (ROOT / "ansible/group_vars/all.example.yml").read_text(encoding="utf-8")
    )
    context = {**defaults, **values}
    context.update(
        {
            "frigate_vm_asr_enabled_resolved": values["asr_enabled"],
            "frigate_vm_asr_port_resolved": values["asr_port"],
            "frigate_vm_asr_max_upload_bytes_resolved": values["asr_max_upload_bytes"],
            "frigate_vm_frigate_backend_port_resolved": values["frigate_backend_port"],
            "frigate_vm_ollama_backend_port_resolved": values["ollama_backend_port"],
            "frigate_vm_asr_backend_port_resolved": values["asr_backend_port"],
            "frigate_vm_docker_gateway_resolved": "172.17.0.1",
        }
    )
    return context


def render(relative_path, context):
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT),
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    environment.filters["bool"] = bool
    environment.filters["urlencode"] = lambda value: quote(str(value), safe="/")
    return environment.get_template(relative_path).render(**context)


class IacTemplateTests(unittest.TestCase):
    def test_declared_dev_dependencies_match_lock(self):
        def pinned_requirements(path):
            result = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^([A-Za-z0-9_-]+)==([^;\s]+)", line)
                if match:
                    result[match.group(1).lower().replace("_", "-")] = match.group(2)
            return result

        declared = pinned_requirements(ROOT / "requirements-dev.txt")
        locked = pinned_requirements(ROOT / "requirements-dev.lock")

        self.assertTrue(declared)
        self.assertEqual(declared, {name: locked.get(name) for name in declared})

    def test_compose_and_frigate_config_render_as_yaml(self):
        context = template_context()
        compose = render(
            "ansible/roles/frigate_vm/templates/docker-compose.yml.j2", context
        )
        config = render(
            "ansible/roles/frigate_vm/templates/frigate-config.yml.j2", context
        )
        compose_data = yaml.safe_load(compose)
        config_data = yaml.safe_load(config)

        ports = compose_data["services"]["frigate"]["ports"]
        self.assertIn("127.0.0.1:18971:8971", ports)
        self.assertEqual(
            "http://host.docker.internal:11435", config_data["genai"]["base_url"]
        )

    def test_nginx_proxies_are_authenticated_and_target_loopback(self):
        context = template_context()
        nginx = render(
            "ansible/roles/frigate_vm/templates/home-ai-proxies.nginx.j2", context
        )

        self.assertNotIn("{{", nginx)
        self.assertEqual(3, nginx.count('auth_basic "Home AI";'))
        self.assertIn("listen 8971 ssl;", nginx)
        self.assertIn("listen 11443 ssl;", nginx)
        self.assertIn("listen 9443 ssl;", nginx)
        self.assertIn("https://127.0.0.1:18971", nginx)
        self.assertIn("http://172.17.0.1:11435", nginx)
        self.assertIn("listen 127.0.0.1:11435", nginx)
        self.assertIn("http://127.0.0.1:19443", nginx)

    def test_disabled_asr_has_no_public_proxy(self):
        context = template_context()
        context["frigate_vm_asr_enabled_resolved"] = False
        nginx = render(
            "ansible/roles/frigate_vm/templates/home-ai-proxies.nginx.j2", context
        )
        self.assertNotIn("listen 9443 ssl;", nginx)

    def test_rtsp_credentials_are_encoded_for_url_userinfo(self):
        context = template_context()
        password_key = "frigate_rtsp_" + "password"
        raw_password = "pa:ss #demo"
        context["frigate_rtsp_user"] = "viewer/name@example"
        context[password_key] = raw_password
        environment = render(
            "ansible/roles/frigate_vm/templates/frigate.env.j2", context
        )

        self.assertIn("FRIGATE_RTSP_USER=viewer%2Fname%40example", environment)
        encoded_password = "FRIGATE_RTSP_PASS" + "WORD=pa%3Ass%20%23demo"
        self.assertIn(encoded_password, environment)
        self.assertNotIn(raw_password, environment)

    def test_nginx_websocket_connection_header_is_conditional(self):
        nginx = render(
            "ansible/roles/frigate_vm/templates/home-ai-proxies.nginx.j2",
            template_context(),
        )

        self.assertIn("map $http_upgrade $home_ai_connection_upgrade", nginx)
        self.assertIn("proxy_set_header Connection $home_ai_connection_upgrade;", nginx)


if __name__ == "__main__":
    unittest.main()
