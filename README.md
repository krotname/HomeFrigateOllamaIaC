# Frigate + Ollama на Hyper-V VM с NVIDIA Tesla

[English](README.en.md)

[![CI](https://github.com/krotname/HomeFrigateOllamaIaC/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/krotname/HomeFrigateOllamaIaC/actions/workflows/ci.yml?query=branch%3Amain)
[![CodeQL](https://github.com/krotname/HomeFrigateOllamaIaC/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/krotname/HomeFrigateOllamaIaC/actions/workflows/codeql.yml?query=branch%3Amain)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/krotname/HomeFrigateOllamaIaC/badge)](https://securityscorecards.dev/viewer/?uri=github.com/krotname/HomeFrigateOllamaIaC)
[![Release](https://img.shields.io/github/v/release/krotname/HomeFrigateOllamaIaC?label=release)](https://github.com/krotname/HomeFrigateOllamaIaC/releases/latest)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![IaC](https://img.shields.io/badge/IaC-Ansible%20%2B%20PowerShell-2f6f9f)](ansible/)

Репозиторий для воспроизводимого разворачивания домашнего video/audio AI стека:
Frigate смотрит камеры, пишет архив и детектит объекты на GPU. Ollama живет в
той же Ubuntu VM и доступна по локальной сети. Отдельный ASR API распознает
аудио через faster-whisper по HTTPS. Windows Server остается основным хостом,
а Linux/CUDA стек живет внутри Ubuntu VM.

Проверенная рабочая конфигурация:

- Windows Server host: `ADLER-WHITE-1W`.
- Hyper-V VM: `frigate-ubuntu`.
- GPU: NVIDIA Tesla P40 через Hyper-V DDA passthrough.
- Frigate: CUDA ffmpeg + ONNX GPU detector YOLOv9-t 320.
- Ollama: `huihui_ai/gpt-oss-abliterated:20b`.
- ASR: `Systran/faster-whisper-large-v3`, CUDA `int8`.
- Frigate HTTPS LAN: `https://192.168.1.138:8971/`.
- Ollama HTTPS LAN: `https://192.168.1.138:11443/`.
- ASR HTTPS LAN: `https://192.168.1.138:9443/`.

## Что Показывает Репозиторий

- Воспроизводимый IaC-подход для домашнего Windows Server + Hyper-V + Ubuntu VM.
- Разделение ответственности между PowerShell, Ansible, Docker Compose и runtime smoke-test.
- GPU passthrough через Hyper-V DDA для постоянной Frigate/Ollama нагрузки.
- Отдельный OpenAI-compatible ASR endpoint для локального распознавания аудио.
- Проверенная клиентская конфигурация OpenCode для локальных моделей Ollama.
- Демонстрационные конфиги с русскими комментариями вместо пустых заглушек.
- CI-проверки для YAML, Ansible, PowerShell, GitHub Actions, CodeQL и OpenSSF Scorecard.
- Документированные governance, reviewer и supply-chain правила для solo-maintained инфраструктуры.

## Зачем Это Нужно

Обычный Frigate на CPU быстро упирается в процессор: декодирование RTSP,
масштабирование кадров, object detection, запись и GenAI-конвейер конкурируют
за одни и те же ядра. В итоге растут задержки, skipped frames и нагрузка на
Windows-хост.

Tesla P40 хорошо подходит для такого домашнего сервера:

- `24 GB` VRAM хватает одновременно под Frigate detector и локальную LLM.
- Карта серверная, рассчитана на постоянную работу 24/7.
- Pascal `compute capability 6.1` все еще поддерживается CUDA/ONNXRuntime в этом
  стеке.
- Frigate разгружает CPU через CUDA decode/scale и ONNX detector.
- Ollama держит модель на GPU, а не в swap/CPU.
- VM изолирует Linux NVIDIA runtime от Windows Server и Docker Desktop.

Идея не в том, что P40 самая новая. Идея в том, что это дешевая серверная карта
с большим объемом памяти, которую можно эффективно использовать для постоянного
видеонаблюдения и локального AI.

## Какие Tesla Подойдут

Проверено в этом репозитории на Tesla P40. Для других карт надо проверять драйвер,
охлаждение, питание, passthrough и поддержку ONNXRuntime/Ollama.

| Карта | Оценка | Комментарий |
| --- | --- | --- |
| Tesla P40 24GB | Лучший бюджетный вариант | Много VRAM, нормальна для inference, требует хорошего обдува. |
| Tesla P4 8GB | Хороша для Frigate | Низкое потребление, но мало VRAM для vision LLM. |
| Tesla P100 16GB | Работоспособна, но не идеальна | Хорошая вычислительная карта, но для quantized inference P40/P4 часто практичнее. |
| Tesla T4 16GB | Отличный вариант | Новее и эффективнее P40, обычно проще с современным AI-стеком. |
| Tesla V100 16/32GB | Сильная, но дорогая | Избыточна для простого домашнего Frigate, хороша для более тяжелых моделей. |
| Tesla M40/M60 | Только как эксперимент | Старее, хуже с современным CUDA/LLM стеком, внимательно проверять поддержку. |
| Tesla K80/K40/K20 | Не рекомендуется | Слишком старые для такого современного CUDA/Ollama/ONNX стека. |

Для compute capability удобно сверяться с официальной таблицей NVIDIA:
[CUDA GPUs](https://developer.nvidia.com/cuda/gpus).

## Технологии

В репозитории намеренно разделены зоны ответственности:

- PowerShell: Windows Server, Hyper-V, автозапуск VM, DDA GPU passthrough.
- Ansible: Ubuntu VM, пакеты, сервисы, шаблоны, сертификат, Docker Compose.
- Docker Compose: runtime Frigate и ASR.

Terraform здесь не главный инструмент, потому что основные ресурсы не в облаке:
они живут в Hyper-V и внутри конкретной Ubuntu VM. Один Docker Compose тоже не
достаточен: Ollama, nginx, сертификаты, модель YOLO и GPU-проверки находятся
снаружи контейнера Frigate.

## Что Внутри

```text
ansible/
  inventory.example.yml
  group_vars/all.example.yml
  playbooks/site.yml
  roles/frigate_vm/
scripts/
  init-local-config.ps1
  hyperv-host-setup.ps1
  invoke-config-backup.ps1
  install-frigate-local-ca.ps1
  smoke-test.ps1
docs/
  architecture.md
  backup-policy.md
  operations.md
  current-state.md
registries/
  backup-registry.csv
  registry-of-registries.csv
frigate/
  .env.example
asr/
  app.py
  docker-compose.yml
  Dockerfile
opencode/
  opencode.example.json
  AGENTS.md
  models/
```

Настройка клиента OpenCode описана в
[docs/opencode-ollama.md](docs/opencode-ollama.md).

## Быстрый Старт

Нужны:

- Windows PowerShell 5.1 или PowerShell 7.
- WinRM over HTTPS-доступ к Windows host `ADLER-WHITE-1W` через `PowerShell.7`.
- SSH-доступ к Ubuntu VM.
- Ansible в WSL/Linux или на другой Unix-like машине управления.
- sudo-пользователь внутри Ubuntu VM.

Штатный доступ к Windows host выполняется только через WinRM HTTPS:

```powershell
$cred = Import-Clixml -LiteralPath 'C:\Users\KRT\.codex\secrets\adler-winrm.credential.xml'
Invoke-Command -ComputerName ADLER-WHITE-1W -UseSSL -ConfigurationName PowerShell.7 -Credential $cred -Authentication Negotiate -ScriptBlock {
    hostname
    whoami
    $PSVersionTable.PSVersion.ToString()
}
```

Raw SSH к Windows host не используется для обычного администрирования; он оставлен
только как аварийный bootstrap/fallback. SSH в этом репозитории остается штатным
каналом только для Ubuntu VM.

1. Создать локальные настройки:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init-local-config.ps1
```

Скрипт создаст:

- `ansible\inventory.yml`
- `ansible\group_vars\all.yml`

Эти файлы игнорируются git. В них будут локальные IP, камеры, RTSP-учетка и
общая basic-auth учетка для LAN API Frigate, Ollama и ASR. Скрипт требует пароль
длиной не менее 12 символов.

2. На Windows Server host настроить VM:

```powershell
$cred = Import-Clixml -LiteralPath 'C:\Users\KRT\.codex\secrets\adler-winrm.credential.xml'
Invoke-Command -ComputerName ADLER-WHITE-1W -UseSSL -ConfigurationName PowerShell.7 -Credential $cred -Authentication Negotiate -FilePath .\scripts\hyperv-host-setup.ps1
```

Если GPU еще не назначена VM, запускать осознанно:

```powershell
$cred = Import-Clixml -LiteralPath 'C:\Users\KRT\.codex\secrets\adler-winrm.credential.xml'
Invoke-Command -ComputerName ADLER-WHITE-1W -UseSSL -ConfigurationName PowerShell.7 -Credential $cred -Authentication Negotiate -FilePath .\scripts\hyperv-host-setup.ps1 -ArgumentList 'frigate-ubuntu', 8, 8GB, 60, 'PCIROOT(0)#PCI(0300)#PCI(0000)', $true
```

3. Развернуть сервисы внутри Ubuntu VM:

```powershell
ansible-playbook -i .\ansible\inventory.yml .\ansible\playbooks\site.yml --ask-become-pass
```

4. Доверить локальный сертификат Frigate/Ollama на Windows-клиенте:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-frigate-local-ca.ps1 `
  -FrigateUrl https://192.168.1.138:8971 `
  -CaCertPath C:\secure-transfer\fullchain.pem
```

5. Проверить весь стек:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1
```

Все три LAN API закрыты basic auth. Для smoke-test задайте одинаковые значения
в `FRIGATE_BASIC_*`, `OLLAMA_BASIC_*` и `ASR_BASIC_*` либо передайте параметры
`-FrigateAuth*`, `-OllamaAuth*` и `-AsrAuth*`. Пароль в репозиторий не кладется.

Ожидаемый результат: `failed_count=0`.

Config-only backups are documented in `docs\backup-policy.md` and tracked in
`registries\backup-registry.csv`.

## Доступ Из Локальной Сети

Сначала установите и проверьте локальный сертификат скриптом
`install-frigate-local-ca.ps1`. Команды ниже намеренно не отключают проверку TLS.

С этой машины Frigate проверяется так:

```powershell
curl.exe -u "$env:FRIGATE_BASIC_USER`:$env:FRIGATE_BASIC_PASSWORD" https://192.168.1.138:8971/api/version
```

Ollama API:

```powershell
curl.exe -u "$env:OLLAMA_BASIC_USER`:$env:OLLAMA_BASIC_PASSWORD" https://192.168.1.138:11443/api/version
```

ASR API:

```powershell
curl.exe -u "$env:ASR_BASIC_USER`:$env:ASR_BASIC_PASSWORD" https://192.168.1.138:9443/health
```

Распознавание аудио:

```powershell
curl.exe -u "$env:ASR_BASIC_USER`:$env:ASR_BASIC_PASSWORD" -X POST "https://192.168.1.138:9443/v1/audio/transcriptions" `
  -F "file=@C:\path\audio.m4a" `
  -F "language=ru" `
  -F "response_format=json"
```

Текстовый запрос к установленной модели:

```powershell
$body = @{
  model = "huihui_ai/gpt-oss-abliterated:20b"
  prompt = "Напиши одно короткое предложение по-русски."
  stream = $false
  think = "low"
  options = @{ num_predict = 256; num_ctx = 2048 }
} | ConvertTo-Json -Depth 4
curl.exe -u "$env:OLLAMA_BASIC_USER`:$env:OLLAMA_BASIC_PASSWORD" `
  --max-time 600 -H "Content-Type: application/json" `
  --data-binary $body https://192.168.1.138:11443/api/generate
```

У модели gpt-oss часть токенов уходит в thinking, поэтому для коротких ответов
лучше ставить `num_predict` не ниже `256`. Холодная загрузка модели на Tesla P40
занимает около 5 минут, поэтому для первого запроса нужен таймаут около
`600` секунд.

## Что Проверяет Smoke-Test

- Windows host identity.
- Hyper-V VM running/autostart/IP.
- Tesla P40 DDA assignment.
- LAN-доступ к Frigate и Ollama.
- Docker и Frigate container health.
- Frigate API.
- ONNX GPU detector: `device=GPU`, `CUDAExecutionProvider`, YOLOv9-t 320.
- CUDA ffmpeg decode/scale pipeline.
- FPS камер и свежие записи.
- Ollama service/API/model.
- ASR HTTPS API/container.
- сетевой путь Frigate container -> Ollama.
- текстовый запрос Ollama -> русский ответ -> `100% GPU`.
- опционально ASR audio transcription, если передан `-AsrSamplePath`.

Последний проверенный production-прогон:

```text
2026-06-28, LAN ports opened
Frigate: https://192.168.1.138:8971/
Ollama: huihui_ai/gpt-oss-abliterated:20b, cold start ~301s, 100% GPU
ASR: https://192.168.1.138:9443/, Systran/faster-whisper-large-v3, CUDA int8
```

Полный proof-снимок лежит в [docs/smoke-test-proof.md](docs/smoke-test-proof.md).

## Релиз

Первый публичный release:
[v0.1.0](https://github.com/krotname/HomeFrigateOllamaIaC/releases/tag/v0.1.0).
В нем опубликованы source archive, `checksums.txt` и GitHub build provenance
attestation для tagged archive.

## Секреты

В репозиторий не попадают:

- `ansible/group_vars/all.yml`
- `.env`
- LAN basic-auth и RTSP-пароли
- private key сертификата
- сгенерированные сертификаты
- `.onnx` / `.pt` модели
- smoke-test отчеты и логи

Если checkout используется не только локально, зашифруйте переменные:

```powershell
ansible-vault encrypt .\ansible\group_vars\all.yml
```

## Качество И Безопасность

- [Security Policy](SECURITY.md) описывает private vulnerability reporting и правила для секретов.
- [Governance](docs/GOVERNANCE.md) фиксирует protected branch baseline и исключения для solo-maintained hygiene changes.
- [Reviewer Guide](docs/REVIEWER_GUIDE.md) перечисляет статические проверки и runtime smoke-test.
- [Smoke-Test Proof](docs/smoke-test-proof.md) фиксирует последний production-прогон `failed_count=0`.
- [Supply Chain Verification](docs/SUPPLY_CHAIN.md) описывает SHA-pinned Actions, pinned dev tools и release attestations.
- [Dependency Policy](docs/DEPENDENCY_POLICY.md) фиксирует текущий baseline Ansible/PowerShell/Frigate/Ollama.
