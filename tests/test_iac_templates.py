import re
import importlib.util
import unittest
from pathlib import Path
from urllib.parse import quote

import jinja2
import yaml


ROOT = Path(__file__).parents[1]
RECORDER_SPEC = importlib.util.spec_from_file_location(
    "apply_recorder_profile", ROOT / "scripts/apply-recorder-profile.py"
)
RECORDER_MODULE = importlib.util.module_from_spec(RECORDER_SPEC)
RECORDER_SPEC.loader.exec_module(RECORDER_MODULE)


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
            "frigate_vm_profile_resolved": "gpu_analytics",
            "frigate_vm_recorder_image_resolved": defaults["frigate_vm_recorder_image"],
            "frigate_vm_ollama_enabled_resolved": True,
            "frigate_vm_asr_enabled_resolved": True,
            "frigate_vm_record_continuous_days_resolved": 3,
            "frigate_vm_record_audio_preset_resolved": "preset-record-generic-audio-copy",
            "frigate_vm_recorder_detect_fps_resolved": 1,
            "frigate_vm_asr_port_resolved": values["asr_port"],
            "frigate_vm_asr_max_upload_bytes_resolved": values["asr_max_upload_bytes"],
            "frigate_vm_frigate_backend_port_resolved": values["frigate_backend_port"],
            "frigate_vm_ollama_backend_port_resolved": values["ollama_backend_port"],
            "frigate_vm_asr_backend_port_resolved": values["asr_backend_port"],
            "frigate_vm_docker_gateway_resolved": "172.17.0.1",
        }
    )
    return context


def camera_watchdog_context():
    context = template_context()
    context["cameras"] = context["cameras"] + [
        {
            "name": "offline_hikvision",
            "host": "192.168.50.33",
            "detect_width": 640,
            "detect_height": 360,
            "detect_fps": 5,
            "enabled": False,
        }
    ]
    context.update(
        {
            "frigate_vm_camera_watchdog_offline_threshold_resolved": 10,
            "frigate_vm_camera_watchdog_online_threshold_resolved": 2,
        }
    )
    return context


def recorder_context():
    context = template_context()
    context.update(
        {
            "frigate_vm_profile_resolved": "recorder",
            "frigate_vm_ollama_enabled_resolved": False,
            "frigate_vm_asr_enabled_resolved": False,
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
    def test_live_transition_preserves_streams_and_removes_gpu_runtime(self):
        source_config = yaml.safe_load(
            render("ansible/roles/frigate_vm/templates/frigate-config.yml.j2", template_context())
        )
        source_compose = yaml.safe_load(
            render("ansible/roles/frigate_vm/templates/docker-compose.yml.j2", template_context())
        )
        for camera in source_config["cameras"].values():
            camera["face_recognition"] = {"enabled": True}

        config = RECORDER_MODULE.recorder_config(source_config, 3, 1)
        compose = RECORDER_MODULE.recorder_compose(
            source_compose, "ghcr.io/blakeblackshear/frigate:stable"
        )
        self.assertEqual(source_config["go2rtc"], config["go2rtc"])
        self.assertEqual(set(source_config["cameras"]), set(config["cameras"]))
        self.assertTrue(
            all("face_recognition" not in camera for camera in config["cameras"].values())
        )
        self.assertNotIn("detectors", config)
        self.assertNotIn("model", config)
        self.assertNotIn("genai", config)
        self.assertEqual(3, config["record"]["continuous"]["days"])
        service = compose["services"]["frigate"]
        self.assertNotIn("runtime", service)
        self.assertNotIn("deploy", service)
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", service["environment"])

    def test_watched_camera_starts_workers_even_if_inventory_says_off(self):
        context = camera_watchdog_context()
        config = yaml.safe_load(
            render("ansible/roles/frigate_vm/templates/frigate-config.yml.j2", context)
        )

        self.assertTrue(config["cameras"]["offline_hikvision"]["enabled"])
        self.assertIn("offline_hikvision_main", config["go2rtc"]["streams"])
        self.assertIn("offline_hikvision_sub", config["go2rtc"]["streams"])
        for camera in ("driveway_hikvision", "garage_hikvision"):
            self.assertTrue(config["cameras"][camera]["enabled"])

    def test_unwatched_disabled_camera_stays_off(self):
        for global_enabled in (True, False):
            with self.subTest(global_enabled=global_enabled):
                context = camera_watchdog_context()
                context["frigate_vm_camera_watchdog_enabled_resolved"] = global_enabled
                if global_enabled:
                    context["cameras"][-1]["watchdog"] = False
                config = yaml.safe_load(
                    render("ansible/roles/frigate_vm/templates/frigate-config.yml.j2", context)
                )
                self.assertFalse(config["cameras"]["offline_hikvision"]["enabled"])

    def test_camera_without_enabled_key_is_on(self):
        config = yaml.safe_load(
            render("ansible/roles/frigate_vm/templates/frigate-config.yml.j2", template_context())
        )

        self.assertEqual(
            {"driveway_hikvision", "garage_hikvision"}, set(config["cameras"])
        )
        for camera in config["cameras"].values():
            self.assertTrue(camera["enabled"])

    def test_camera_watchdog_service_lists_every_watched_camera(self):
        service = render(
            "ansible/roles/frigate_vm/templates/krt-camera-watchdog.service.j2",
            camera_watchdog_context(),
        )
        # systemd splits an unquoted Environment= value on spaces, so the whole
        # assignment has to stay inside one pair of quotes.
        target_line = next(
            line
            for line in service.splitlines()
            if line.startswith('Environment="CAMERA_WATCHDOG_TARGETS=')
        )
        self.assertTrue(target_line.endswith('"'), target_line)
        targets = target_line.removeprefix(
            'Environment="CAMERA_WATCHDOG_TARGETS='
        ).removesuffix('"')

        self.assertEqual(
            [
                "driveway_hikvision=192.168.50.31",
                "garage_hikvision=192.168.50.32",
                "offline_hikvision=192.168.50.33",
            ],
            targets.split(),
        )
        self.assertIn("Environment=CAMERA_WATCHDOG_OFFLINE_THRESHOLD=10", service)
        self.assertIn("Environment=CAMERA_WATCHDOG_ONLINE_THRESHOLD=2", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET", service)
        self.assertIn("RuntimeDirectoryPreserve=yes", service)

    def test_camera_watchdog_service_honours_the_per_camera_opt_out(self):
        context = camera_watchdog_context()
        context["cameras"][-1] = {**context["cameras"][-1], "watchdog": False}
        service = render(
            "ansible/roles/frigate_vm/templates/krt-camera-watchdog.service.j2", context
        )

        self.assertNotIn("offline_hikvision=", service)
        self.assertIn(
            'Environment="CAMERA_WATCHDOG_TARGETS='
            'driveway_hikvision=192.168.50.31 garage_hikvision=192.168.50.32"',
            service,
        )

    def test_camera_watchdog_script_toggles_only_after_repeated_probes(self):
        script = (
            ROOT / "ansible/roles/frigate_vm/files/camera-watchdog.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("CAMERA_WATCHDOG_OFFLINE_THRESHOLD:-10", script)
        self.assertIn("CAMERA_WATCHDOG_ONLINE_THRESHOLD:-2", script)
        self.assertIn("/api/camera/$name/set/enabled", script)
        self.assertNotIn("/api/config/set", script)
        self.assertIn("(( online >= ONLINE_THRESHOLD ))", script)
        self.assertIn("(( offline < OFFLINE_THRESHOLD ))", script)
        self.assertIn("it is the last enabled camera", script)
        self.assertIn("leaving the camera list alone", script)

    def test_ollama_watchdog_recovery_is_conservative(self):
        role_files = ROOT / "ansible/roles/frigate_vm/files"
        script = (role_files / "ollama-watchdog.sh").read_text(encoding="utf-8")
        service = render(
            "ansible/roles/frigate_vm/templates/ollama-watchdog.service.j2",
            template_context(),
        )

        self.assertIn("OLLAMA_WATCHDOG_FAILURE_THRESHOLD:-2", script)
        self.assertIn("OLLAMA_WATCHDOG_COOLDOWN_SECONDS:-900", script)
        self.assertIn("restart deferred until the failure is confirmed", script)
        self.assertIn("restart suppressed by ${COOLDOWN_SECONDS}s cooldown", script)
        self.assertIn("RuntimeDirectory=krt-ollama-watchdog", service)
        self.assertIn("RuntimeDirectoryPreserve=yes", service)

    def test_ollama_watchdog_probes_deployed_model_and_port(self):
        service = render(
            "ansible/roles/frigate_vm/templates/ollama-watchdog.service.j2",
            template_context(),
        )

        self.assertIn(
            "Environment=OLLAMA_WATCHDOG_URL=http://127.0.0.1:11435", service
        )
        self.assertIn(
            "Environment=OLLAMA_WATCHDOG_PROBE_MODEL=huihui_ai/gpt-oss-abliterated:20b",
            service,
        )

    def test_container_watchdog_allowlist_follows_asr_setting(self):
        script = (
            ROOT / "ansible/roles/frigate_vm/files/container-watchdog.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("CONTAINER_WATCHDOG_CONTAINERS", script)

        context = template_context()
        enabled = render(
            "ansible/roles/frigate_vm/templates/krt-container-watchdog.service.j2",
            context,
        )
        self.assertIn('Environment="CONTAINER_WATCHDOG_CONTAINERS=frigate asr"\n', enabled)

        context["frigate_vm_asr_enabled_resolved"] = False
        disabled = render(
            "ansible/roles/frigate_vm/templates/krt-container-watchdog.service.j2",
            context,
        )
        self.assertIn('Environment="CONTAINER_WATCHDOG_CONTAINERS=frigate"\n', disabled)

    def test_container_watchdog_timeout_covers_sequential_recovery(self):
        script = (
            ROOT / "ansible/roles/frigate_vm/files/container-watchdog.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("RECOVERY_TIMEOUT_SECONDS=180", script)
        # Отказ docker restart/start не должен обрывать обход остальных контейнеров.
        self.assertIn("docker restart failed for container", script)
        self.assertIn("docker start failed for container", script)

        context = template_context()
        enabled = render(
            "ansible/roles/frigate_vm/templates/krt-container-watchdog.service.j2",
            context,
        )
        # frigate и asr ждутся последовательно: 180 с на каждый плюс запас.
        self.assertIn("TimeoutStartSec=420\n", enabled)

        context["frigate_vm_asr_enabled_resolved"] = False
        disabled = render(
            "ansible/roles/frigate_vm/templates/krt-container-watchdog.service.j2",
            context,
        )
        self.assertIn("TimeoutStartSec=240\n", disabled)

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

    def test_black_gpu_selectors_use_runtime_uuids_with_legacy_defaults(self):
        selectors = (
            ("asr", "asr", "BLACK_ASR_GPU_UUID", "0"),
            (
                "ocr", "ocr-llm", "BLACK_OCR_GPU_UUID",
                "GPU-9b076900-4700-5e08-9abd-c67fb39c6026",
            ),
        )
        for directory, service_name, variable, default in selectors:
            with self.subTest(service=service_name):
                compose = yaml.safe_load(
                    (ROOT / directory / "docker-compose.black.yml").read_text(
                        encoding="utf-8"
                    )
                )
                service = compose["services"][service_name]
                # Compose's :- keeps the legacy default for unset AND empty values;
                # a UUID supplied by VpnOps through --env-file selects either P40.
                self.assertEqual(
                    f"${{{variable}:-{default}}}",
                    service["environment"]["CUDA_VISIBLE_DEVICES"],
                )
                self.assertEqual("all", service["gpus"])

    def test_black_asr_keeps_cuda_model_and_lan_tls_config(self):
        compose = yaml.safe_load(
            (ROOT / "asr/docker-compose.black.yml").read_text(encoding="utf-8")
        )
        service = compose["services"]["asr"]
        self.assertEqual("host", service["network_mode"])
        self.assertNotIn("ports", service)
        for name, value in {
            "ASR_MODEL": "Systran/faster-whisper-large-v3",
            "ASR_DEVICE": "cuda",
            "ASR_COMPUTE_TYPE": "int8",
            "ASR_PORT": "19443",
            "ASR_CERT_FILE": "/certs/fullchain.pem",
            "ASR_KEY_FILE": "/certs/privkey.pem",
            "ASR_MAX_CONCURRENT_TRANSCRIPTIONS": "1",
        }.items():
            with self.subTest(setting=name):
                self.assertEqual(value, service["environment"][name])

    def test_black_ocr_keeps_model_on_loopback_and_lan_tls_config(self):
        compose = yaml.safe_load(
            (ROOT / "ocr/docker-compose.black.yml").read_text(encoding="utf-8")
        )
        for service in compose["services"].values():
            self.assertEqual("host", service["network_mode"])
            self.assertNotIn("ports", service)
        command = compose["services"]["ocr-llm"]["command"]
        for flag, value in {
            "--host": "127.0.0.1",
            "--port": "18090",
            "--model": "/models/Qwen3-VL-2B-Instruct-Q8_0.gguf",
            "--mmproj": "/models/mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf",
            "--n-gpu-layers": "99",
            "--parallel": "1",
        }.items():
            with self.subTest(flag=flag):
                self.assertEqual(value, command[command.index(flag) + 1])
        environment = compose["services"]["ocr"]["environment"]
        for name, value in {
            "OCR_LLM_URL": "http://127.0.0.1:18090",
            "OCR_MODEL_NAME": "Qwen3-VL-2B-Instruct-Q8_0",
            "OCR_PORT": "443",
            "OCR_CERT_FILE": "/certs/fullchain.pem",
            "OCR_KEY_FILE": "/certs/privkey.pem",
            "OCR_MAX_CONCURRENT_JOBS": "1",
        }.items():
            with self.subTest(setting=name):
                self.assertEqual(value, environment[name])

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

    def test_recorder_profile_has_no_gpu_or_analytics_runtime(self):
        context = recorder_context()
        compose = yaml.safe_load(
            render("ansible/roles/frigate_vm/templates/docker-compose.yml.j2", context)
        )
        config = yaml.safe_load(
            render("ansible/roles/frigate_vm/templates/frigate-config.yml.j2", context)
        )
        service = compose["services"]["frigate"]

        self.assertEqual("ghcr.io/blakeblackshear/frigate:stable", service["image"])
        self.assertNotIn("runtime", service)
        self.assertNotIn("deploy", service)
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", service["environment"])
        self.assertNotIn("nvidia-smi", " ".join(service["healthcheck"]["test"]))
        self.assertNotIn("detectors", config)
        self.assertNotIn("model", config)
        self.assertNotIn("genai", config)
        self.assertFalse(config["detect"]["enabled"])
        self.assertFalse(config["motion"]["enabled"])
        self.assertFalse(config["birdseye"]["enabled"])
        self.assertFalse(config["snapshots"]["enabled"])
        self.assertEqual(3, config["record"]["continuous"]["days"])
        self.assertEqual(
            "preset-record-generic-audio-copy", config["ffmpeg"]["output_args"]["record"]
        )
        for camera in config["cameras"].values():
            self.assertEqual(1, camera["detect"]["fps"])
            self.assertFalse(camera["motion"]["enabled"])

    def test_recorder_nginx_exposes_only_frigate(self):
        nginx = render(
            "ansible/roles/frigate_vm/templates/home-ai-proxies.nginx.j2",
            recorder_context(),
        )

        self.assertEqual(1, nginx.count('auth_basic "Home AI";'))
        self.assertIn("listen 8971 ssl;", nginx)
        self.assertNotIn("listen 11443 ssl;", nginx)
        self.assertNotIn("listen 9443 ssl;", nginx)

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
