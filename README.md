# Frigate + Ollama на Hyper-V VM с NVIDIA Tesla

[![CI](https://github.com/krotname/HomeFrigateOllamaIaC/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/krotname/HomeFrigateOllamaIaC/actions/workflows/ci.yml?query=branch%3Amain)
[![CodeQL](https://github.com/krotname/HomeFrigateOllamaIaC/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/krotname/HomeFrigateOllamaIaC/actions/workflows/codeql.yml?query=branch%3Amain)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/krotname/HomeFrigateOllamaIaC/badge)](https://securityscorecards.dev/viewer/?uri=github.com/krotname/HomeFrigateOllamaIaC)
[![Release](https://img.shields.io/github/v/release/krotname/HomeFrigateOllamaIaC?label=release)](https://github.com/krotname/HomeFrigateOllamaIaC/releases/latest)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![IaC](https://img.shields.io/badge/IaC-Ansible%20%2B%20PowerShell-2f6f9f)](ansible/)

Репозиторий для воспроизводимого разворачивания домашнего video AI стека:
Frigate смотрит камеры, пишет архив, детектит объекты на GPU и отправляет кадры
в Ollama vision-модель. Windows Server остается основным хостом, а Linux/CUDA
стек живет внутри Ubuntu VM.

Проверенная рабочая конфигурация:

- Windows Server host: `ADLER-WHITE-1W`.
- Hyper-V VM: `frigate-ubuntu`.
- GPU: NVIDIA Tesla P40 через Hyper-V DDA passthrough.
- Frigate: CUDA ffmpeg + ONNX GPU detector YOLOv9-t 320.
- Ollama: `qwen2.5vl:3b`.
- Frigate HTTPS: `8971`.
- Ollama HTTPS proxy: `11443`.

## Что Показывает Репозиторий

- Воспроизводимый IaC-подход для домашнего Windows Server + Hyper-V + Ubuntu VM.
- Разделение ответственности между PowerShell, Ansible, Docker Compose и runtime smoke-test.
- GPU passthrough через Hyper-V DDA для постоянной Frigate/Ollama нагрузки.
- Демонстрационные конфиги с русскими комментариями вместо пустых заглушек.
- CI-проверки для YAML, Ansible, PowerShell, GitHub Actions, CodeQL и OpenSSF Scorecard.
- Документированные governance, reviewer и supply-chain правила для solo-maintained инфраструктуры.

## Зачем Это Нужно

Обычный Frigate на CPU быстро упирается в процессор: декодирование RTSP,
масштабирование кадров, object detection, запись и GenAI-конвейер конкурируют
за одни и те же ядра. В итоге растут задержки, skipped frames и нагрузка на
Windows-хост.

Tesla P40 хорошо подходит для такого домашнего сервера:

- `24 GB` VRAM хватает одновременно под Frigate detector и небольшую vision LLM.
- Карта серверная, рассчитана на постоянную работу 24/7.
- Pascal `compute capability 6.1` все еще поддерживается CUDA/ONNXRuntime в этом
  стеке.
- Frigate разгружает CPU через CUDA decode/scale и ONNX detector.
- Ollama держит vision-модель на GPU, а не в swap/CPU.
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
- Docker Compose: runtime Frigate.

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
  install-frigate-local-ca.ps1
  smoke-test.ps1
docs/
  architecture.md
  operations.md
  current-state.md
frigate/
  .env.example
```

## Быстрый Старт

Нужны:

- Windows PowerShell 5.1 или PowerShell 7.
- SSH-доступ к Windows host и Ubuntu VM.
- Ansible в WSL/Linux или на другой Unix-like машине управления.
- sudo-пользователь внутри Ubuntu VM.

1. Создать локальные настройки:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init-local-config.ps1
```

Скрипт создаст:

- `ansible\inventory.yml`
- `ansible\group_vars\all.yml`

Эти файлы игнорируются git. В них будут локальные IP, камеры и RTSP-учетка.

2. На Windows Server host настроить VM:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\hyperv-host-setup.ps1
```

Если GPU еще не назначена VM, запускать осознанно:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\hyperv-host-setup.ps1 -AssignGpu
```

3. Развернуть сервисы внутри Ubuntu VM:

```powershell
ansible-playbook -i .\ansible\inventory.yml .\ansible\playbooks\site.yml --ask-become-pass
```

4. Доверить локальный сертификат Frigate/Ollama на Windows-клиенте:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-frigate-local-ca.ps1 -FrigateUrl https://192.168.1.138:8971
```

5. Проверить весь стек:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-test.ps1
```

Ожидаемый результат: `failed_count=0`.

## Что Проверяет Smoke-Test

- Windows host identity.
- Hyper-V VM running/autostart/IP.
- Tesla P40 DDA assignment.
- доверенный TLS для Frigate и Ollama HTTPS proxy.
- Docker и Frigate container health.
- Frigate API.
- ONNX GPU detector: `device=GPU`, `CUDAExecutionProvider`, YOLOv9-t 320.
- CUDA ffmpeg decode/scale pipeline.
- FPS камер и свежие записи.
- Ollama service/API/model.
- сетевой путь Frigate container -> Ollama.
- live кадр из Frigate -> Ollama vision model -> русский ответ -> `100% GPU`.

Последний проверенный production-прогон:

```text
2026-06-15 09:33, failed_count=0
ONNX detector: GPU, inference ~8.16 ms
Ollama vision: qwen2.5vl:3b, 100% GPU
```

## Секреты

В репозиторий не попадают:

- `ansible/group_vars/all.yml`
- `.env`
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
- [Supply Chain Verification](docs/SUPPLY_CHAIN.md) описывает SHA-pinned Actions, pinned dev tools и release attestations.
- [Dependency Policy](docs/DEPENDENCY_POLICY.md) фиксирует текущий baseline Ansible/PowerShell/Frigate/Ollama.
