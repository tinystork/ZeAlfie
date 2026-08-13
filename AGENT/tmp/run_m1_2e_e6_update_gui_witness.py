#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = ROOT / "AGENT" / "tmp" / "m1_2e_e6_update_gui_witness"
RUNTIME_ROOT = RUN_ROOT / "runtime_root"
SELECTION_DIR = RUN_ROOT / "selection"
WORK_ROOT = RUN_ROOT / "work"
CACHE_DIR = RUN_ROOT / "cache"
PRODUCT_ID = "zesolver"
OWNER = "tinystork"
REPO = "ZeSolver"
REF = "main"
BOOTSTRAP_A_SHA = os.environ.get("ZEALFIE_E6_BOOTSTRAP_A_SHA", "2a8806b2ffc265ca582ba105de88f5457578d078").strip().lower()

for k, v in {
    "QT_QPA_PLATFORM": "offscreen",
    "TMPDIR": str(CACHE_DIR / "tmp"),
    "TEMP": str(CACHE_DIR / "tmp"),
    "TMP": str(CACHE_DIR / "tmp"),
    "PIP_CACHE_DIR": str(CACHE_DIR / "pip"),
    "ZEALFIE_RUNTIME_ROOT": str(RUNTIME_ROOT),
}.items():
    os.environ[k] = v
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QThread, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from zealfie.app import SelectionStore, ZeAlfieService  # noqa: E402
from zealfie.app.updates import UpdateStatus  # noqa: E402
from zealfie.dependencies.pip_acquirer import PipWheelhouseAcquirer  # noqa: E402
from zealfie.gui.main_window import ZeAlfieMainWindow  # noqa: E402
from zealfie.runtime.layout import RuntimeLayout  # noqa: E402
from zealfie.runtime.manager import SharedRuntime  # noqa: E402
from zealfie.sources.github import GitHubArchiveFetcher, GitHubSourceRefResolver  # noqa: E402


def thread_id() -> int:
    return int(id(QThread.currentThread()))


def proc_state(pid: int) -> str | None:
    try:
        for line in (Path('/proc') / str(pid) / 'status').read_text().splitlines():
            if line.startswith('State:'):
                return line.split(':', 1)[1].strip()
    except Exception:
        return None
    return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running(pid: int) -> bool:
    st = proc_state(pid)
    return st is not None and not st.startswith('Z')


def terminate(pid: int) -> dict[str, object]:
    info = {'pid': pid, 'state_before': proc_state(pid), 'state_after_term': None, 'state_after_kill': None, 'running_after': False}
    if not pid or not alive(pid):
        return info
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return info
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if not running(pid):
            info['state_after_term'] = proc_state(pid)
            info['running_after'] = False
            return info
        time.sleep(0.1)
    info['state_after_term'] = proc_state(pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if not running(pid):
            break
        time.sleep(0.1)
    info['state_after_kill'] = proc_state(pid)
    info['running_after'] = running(pid)
    return info


def proc_env(pid: int) -> dict[str, str]:
    try:
        raw = (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0')
    except Exception:
        return {}
    out = {}
    for item in raw:
        if b'=' not in item:
            continue
        k, v = item.split(b'=', 1)
        ks = k.decode('utf-8', 'replace')
        if ks in {'ZESOLVER_EMBEDDED_HOST', 'QT_QPA_PLATFORM', 'DISPLAY'}:
            out[ks] = v.decode('utf-8', 'replace')
    return out


def proc_cmd(pid: int) -> str:
    try:
        return (Path('/proc') / str(pid) / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
    except Exception:
        return ''


def disk(path: Path) -> dict[str, int]:
    path.mkdir(parents=True, exist_ok=True)
    u = shutil.disk_usage(path)
    return {'total': u.total, 'used': u.used, 'free': u.free}


def dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for p in path.rglob('*'):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


def state_for(shell, pid: str):
    for p in shell.products:
        if p.product_id == pid:
            return p
    raise RuntimeError(f'missing product state {pid}')


def process_until(cond, timeout_s: float, interval_ms: int = 50) -> bool:
    app = QApplication.instance(); assert app is not None
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        QThread.msleep(interval_ms)
    app.processEvents()
    return bool(cond())


def button(card) -> str:
    return card._action_button.text()


def update_button(card) -> str | None:
    return None if card._update_button is None else card._update_button.text()


def status(card) -> str:
    return card._status_label.text()


def update_status(card) -> str:
    if hasattr(card, "update_status_text"):
        return str(card.update_status_text)
    label = getattr(card, "_update_status_label", None)
    return "" if label is None else label.text()


def latest_commit() -> str:
    url = f'https://api.github.com/repos/{OWNER}/{REPO}/commits/{REF}'
    headers = {'User-Agent': 'ZeAlfie-E6-Witness/1.0'}
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    with urlopen(Request(url, headers=headers), timeout=30) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    latest = str(data['sha']).lower()
    return latest


class FixedResolver:
    def __init__(self, sha: str) -> None:
        self.sha = sha
        self.calls: list[tuple[str, str, str]] = []
    def __call__(self, owner: str, repo: str, ref: str) -> str:
        self.calls.append((owner, repo, ref))
        return self.sha


def classify(stage: str, exc: BaseException) -> str:
    s = f'{type(exc).__name__}: {exc}'.lower()
    if 'bootstrap' in stage or 'initial' in s:
        return 'BOOTSTRAP_A'
    if 'update available' in s or 'check' in s:
        return 'CHECK_B'
    if 'click' in s or 'update_requested' in s:
        return 'GUI_CLICK'
    if 'thread' in s and 'worker' in s:
        return 'WORKER'
    if 'timer' in s or 'ticks' in s or 'main thread' in s:
        return 'RESPONSIVENESS'
    if 'github' in s or 'archive' in s or 'source' in s or 'wheel build' in s:
        return 'GITHUB_BUILD'
    if 'pip' in s or 'dependency' in s or 'acquisition' in s:
        return 'PYPI_DEPS'
    if 'lock' in s or 'plan' in s or ('runtime' in s and 'absent' not in s):
        return 'RUNTIME_TRANSACTION'
    if 'apply' in s or 'activate' in s or 'slot' in s:
        return 'APPLY_ACTIVATE'
    if 'provenance' in s:
        return 'PROVENANCE'
    if 'refresh' in s or 'up to date' in s:
        return 'REFRESH_UP_TO_DATE'
    if 'spawn' in s or 'launch' in s or 'pid' in s:
        return 'LAUNCH'
    return stage


def main() -> int:
    if RUN_ROOT.exists():
        shutil.rmtree(RUN_ROOT)
    for d in (RUNTIME_ROOT, SELECTION_DIR, WORK_ROOT, CACHE_DIR / 'tmp', CACHE_DIR / 'pip'):
        d.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        'status': 'FAIL', 'stage': 'init', 'repo': str(ROOT),
        'run_root': str(RUN_ROOT), 'runtime_root': str(RUNTIME_ROOT),
        'selection_path': str(SELECTION_DIR / 'desired-products.toml'),
        'work_root': str(WORK_ROOT), 'cache_root': str(CACHE_DIR),
        'events': [], 'threading': {}, 'responsiveness': {}, 'bootstrap': {},
        'update': {}, 'ux': {}, 'launch': {}, 'teardown': {},
        'disk_before': disk(RUN_ROOT),
        'slash_tmp_acq_before': sorted(str(p) for p in Path('/tmp').glob('zealfie-acq-*')),
    }

    def log(msg: str) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

    def event(name: str, **kw: Any) -> None:
        rec = {'t': time.monotonic(), 'name': name, **kw}
        result['events'].append(rec)
        log('EVENT ' + name + ' ' + json.dumps(kw, ensure_ascii=False, default=str))

    app = QApplication.instance() or QApplication([])
    main_tid = thread_id(); result['threading']['main_thread_id'] = main_tid; event('qapp_ready', main_thread_id=main_tid)
    runtime = SharedRuntime(layout=RuntimeLayout(root=RUNTIME_ROOT))
    store = SelectionStore(path=SELECTION_DIR / 'desired-products.toml')
    acquirer = PipWheelhouseAcquirer()
    service = ZeAlfieService(runtime=runtime, selection_store=store, acquirer=acquirer)
    fetcher = GitHubArchiveFetcher()
    real_resolver = GitHubSourceRefResolver()
    window = None
    launch_pid = None

    try:
        result['stage'] = 'resolve_A_B'
        latest_b = latest_commit()
        old_a = BOOTSTRAP_A_SHA
        if latest_b == old_a:
            raise RuntimeError('A and B commits are identical')
        result['update'].update(owner=OWNER, repo=REPO, requested_ref=REF, commit_a=old_a, commit_b=latest_b)
        event('resolved_commits', commit_a=old_a, commit_b=latest_b)

        result['stage'] = 'bootstrap_A_direct_install'
        fixed_resolver = FixedResolver(old_a)
        progress_a: list[dict[str, Any]] = []
        def progress_bootstrap(progress) -> None:
            rec = {
                'phase': getattr(getattr(progress, 'phase', None), 'value', str(getattr(progress, 'phase', None))),
                'percent': int(getattr(progress, 'percent', -1)),
                'message': str(getattr(progress, 'message', '')),
                'thread_id': thread_id(),
            }
            progress_a.append(rec)
            event('bootstrap_progress', **rec)
        t_boot = time.monotonic()
        boot = service.install_product(PRODUCT_ID, resolver=fixed_resolver, fetcher=fetcher, work_root=WORK_ROOT / 'bootstrap_a', progress_callback=progress_bootstrap)
        result['bootstrap'].update(success=boot.success, reason=boot.reason, active_slot_id=boot.active_slot_id, duration_s=time.monotonic()-t_boot, resolver_calls=fixed_resolver.calls, progress=progress_a)
        if not boot.success:
            raise RuntimeError(f'bootstrap A install failed: {boot.reason}')
        prov_a = service.product_provenance(PRODUCT_ID)
        if prov_a is None:
            raise RuntimeError('bootstrap A provenance missing')
        result['bootstrap']['provenance_a'] = {
            'product_id': prov_a.product_id, 'version': prov_a.version,
            'source_owner': prov_a.source_owner, 'source_repo': prov_a.source_repo,
            'requested_ref': prov_a.requested_ref, 'commit_sha': prov_a.commit_sha,
            'wheel_sha256': prov_a.wheel_sha256,
        }
        if prov_a.commit_sha != old_a or prov_a.requested_ref != REF:
            raise RuntimeError(f'bad bootstrap provenance A: {result["bootstrap"]["provenance_a"]}')
        st_a = state_for(service.collect_product_state(), PRODUCT_ID)
        if not (st_a.installed and st_a.launchable):
            raise RuntimeError(f'bootstrap A product not launchable: {st_a}')
        event('bootstrap_a_complete', commit_a=prov_a.commit_sha, active_slot_id=boot.active_slot_id, version=prov_a.version)

        result['stage'] = 'product_shell_gui'
        update_call = {'count': 0}
        orig_update = service.update_product
        orig_spawn = service.spawn_component
        spawn_rec: dict[str, Any] = {}
        def wrap_update(pid, **kw):
            update_call['count'] += 1
            tid = thread_id()
            result['threading'].update(worker_thread_id=tid, threads_differ=(tid != main_tid))
            orig_progress_cb = kw.get('progress_callback')
            progress_events = result.setdefault('progress_events', [])
            def recording_progress(progress):
                rec = {
                    't': time.monotonic(),
                    'phase': getattr(getattr(progress, 'phase', None), 'value', str(getattr(progress, 'phase', None))),
                    'percent': int(getattr(progress, 'percent', -1)),
                    'message': str(getattr(progress, 'message', '')),
                    'thread_id': thread_id(),
                }
                progress_events.append(rec)
                event('update_backend_progress', **rec)
                if orig_progress_cb is not None:
                    orig_progress_cb(progress)
            kw['progress_callback'] = recording_progress
            event('service_update_product_start', product_id=pid, thread_id=tid, had_original_progress_callback=orig_progress_cb is not None)
            t = time.monotonic()
            try:
                return orig_update(pid, **kw)
            finally:
                event('service_update_product_end', product_id=pid, thread_id=thread_id(), duration_s=time.monotonic()-t)
        def wrap_spawn(pid, **kw):
            event('spawn_component_start', product_id=pid, thread_id=thread_id())
            sp = orig_spawn(pid, **kw)
            spawn_rec.update(component_id=sp.component_id, pid=sp.pid, executable=str(sp.executable) if sp.executable else None, command=list(sp.command) if sp.command else None)
            event('spawn_component_done', product_id=pid, pid=sp.pid, executable=spawn_rec.get('executable'))
            return sp
        service.update_product = wrap_update  # type: ignore[method-assign]
        service.spawn_component = wrap_spawn  # type: ignore[method-assign]

        check_fn = lambda product_id: service.check_product_update(product_id, resolver=real_resolver)
        window = ZeAlfieMainWindow(service=service, resolver=real_resolver, fetcher=fetcher, work_root=WORK_ROOT / 'update_b', check_fn=check_fn)
        window.show(); app.processEvents()
        card = window._cards[PRODUCT_ID]
        card.update_requested.connect(lambda pid: event('card_update_requested', product_id=pid, thread_id=thread_id()))

        result['stage'] = 'wait_update_available'
        ok = process_until(lambda: 'Update available' in update_status(card) and card._update_button is not None and card._update_button.isEnabled(), 60, 50)
        upd = service.check_product_update(PRODUCT_ID, resolver=real_resolver)
        pre_gui = {
            'button': button(card), 'button_enabled': card._action_button.isEnabled(),
            'update_button': update_button(card), 'update_button_enabled': None if card._update_button is None else card._update_button.isEnabled(),
            'update_status_label': update_status(card),
            'update_status': str(upd.status),
            'installed_commit_sha': upd.installed_commit_sha,
            'latest_commit_sha': upd.latest_commit_sha,
        }
        result['update']['pre_gui'] = pre_gui; event('pre_gui_update_state', **pre_gui)
        if not ok or upd.status is not UpdateStatus.UPDATE_AVAILABLE:
            raise RuntimeError(f'GUI did not reach Update available: {pre_gui}')
        if upd.installed_commit_sha != old_a or upd.latest_commit_sha != latest_b:
            raise RuntimeError(f'pre-update A/B mismatch: {pre_gui}, expected A={old_a} B={latest_b}')
        if card._update_button is None or not card._update_button.isEnabled() or 'Mettre' not in card._update_button.text():
            raise RuntimeError(f'GUI update button unavailable: {pre_gui}')

        result['stage'] = 'click_update_gui'
        ticks: list[float] = []
        timer = QTimer(window); timer.setInterval(100); timer.timeout.connect(lambda: ticks.append(time.monotonic())); timer.start()
        click_start = time.monotonic()
        card._update_button.click(); app.processEvents()
        event('update_clicked', update_button=update_button(card), install_active=window.install_active)
        if not window.install_active or window._install_thread is None or window._install_worker is None:
            raise RuntimeError('click Mettre à jour did not start worker')
        if getattr(window._install_worker, '_operation', None) != 'update':
            raise RuntimeError(f'worker operation is not update: {getattr(window._install_worker, "_operation", None)!r}')
        window._install_worker.install_succeeded.connect(lambda pid: event('worker_update_succeeded_signal', product_id=pid, thread_id=thread_id()))
        window._install_worker.install_failed.connect(lambda pid, msg: event('worker_update_failed_signal', product_id=pid, message=msg, thread_id=thread_id()))
        window._install_worker.finished.connect(lambda: event('worker_finished_signal', thread_id=thread_id()))
        window._install_worker.progress.connect(lambda p: event(
            'worker_progress_signal',
            phase=getattr(getattr(p, 'phase', None), 'value', str(getattr(p, 'phase', None))),
            percent=int(getattr(p, 'percent', -1)),
            message=str(getattr(p, 'message', '')),
            thread_id=thread_id(),
            card_status=status(card),
        ))
        busy = {
            'update_button': update_button(card),
            'update_button_enabled': None if card._update_button is None else card._update_button.isEnabled(),
            'launch_button': button(card),
            'launch_button_enabled': card._action_button.isEnabled(),
            'progress_visible': window._install_progress_bar.isVisible(),
            'status_global': window._status_label.text(),
            'card_status': status(card),
            'refresh_enabled': window._refresh_action.isEnabled(),
            'install_active': window.install_active,
        }
        result['ux']['busy'] = busy; event('busy_snapshot', **busy)

        result['stage'] = 'wait_update_worker'
        done = process_until(lambda: not window.install_active, 1200, 50)
        timer.stop(); app.processEvents()
        if not done:
            raise TimeoutError('real GUI update did not finish within 1200s')
        install_end = time.monotonic()
        td = [t for t in ticks if click_start <= t <= install_end]
        intervals = [b-a for a,b in zip(ticks,ticks[1:])]
        resp = {'timer_interval_ms': 100, 'total_ticks': len(ticks), 'ticks_during_update': len(td), 'max_interval_s': max(intervals) if intervals else None, 'timer_fired_before_update_end': bool(td), 'update_product_calls': update_call['count']}
        result['responsiveness'] = resp; event('responsiveness_summary', **resp)
        if len(td) < 2:
            raise RuntimeError(f'insufficient Qt ticks during update: {resp}')
        if update_call['count'] != 1:
            raise RuntimeError(f'expected exactly one service.update_product call through worker, got {update_call["count"]}')
        if not result['threading'].get('threads_differ'):
            raise RuntimeError(f'worker did not differ from main thread: {result["threading"]}')

        result['stage'] = 'verify_B_up_to_date'
        ok2 = process_until(lambda: update_status(card) == 'Up to date', 90, 50)
        upd2 = service.check_product_update(PRODUCT_ID, resolver=real_resolver)
        prov_b = service.product_provenance(PRODUCT_ID)
        if prov_b is None:
            raise RuntimeError('post-update provenance missing')
        st_b = state_for(service.collect_product_state(), PRODUCT_ID)
        progress = result.get('progress_events', [])
        phases = [p.get('phase') for p in progress]
        percents = [int(p.get('percent', -1)) for p in progress]
        progress_summary = {
            'count': len(progress), 'phases': phases, 'percents': percents,
            'monotone': all(b >= a for a, b in zip(percents, percents[1:])),
            'bounded': all(0 <= p <= 100 for p in percents),
            'completed_count': phases.count('completed'),
            'last_phase': phases[-1] if phases else None,
            'last_percent': percents[-1] if percents else None,
            'worker_signal_count': len([e for e in result['events'] if e.get('name') == 'worker_progress_signal']),
        }
        final = {
            'state_installed': st_b.installed, 'state_launchable': st_b.launchable,
            'button': button(card), 'button_enabled': card._action_button.isEnabled(),
            'update_button': update_button(card), 'update_status_label': update_status(card),
            'update_status': str(upd2.status),
            'up_to_date_seen': ok2,
            'provenance_b': {
                'product_id': prov_b.product_id, 'version': prov_b.version,
                'source_owner': prov_b.source_owner, 'source_repo': prov_b.source_repo,
                'requested_ref': prov_b.requested_ref, 'commit_sha': prov_b.commit_sha,
                'wheel_sha256': prov_b.wheel_sha256,
            },
            'active_slot_id': runtime.status().active_slot_id,
            'progress_summary': progress_summary,
        }
        result['update']['final'] = final; event('post_update_final_state', **final)
        required = {'preparing', 'resolving_source', 'downloading_source', 'building_product', 'acquiring_dependencies', 'planning_runtime', 'installing_runtime', 'validating', 'activating', 'completed'}
        if not required.issubset(set(phases)):
            raise RuntimeError(f'missing update progress phases: required={sorted(required)} observed={phases}')
        if not (progress_summary['monotone'] and progress_summary['bounded'] and progress_summary['completed_count'] == 1 and progress_summary['last_percent'] == 100):
            raise RuntimeError(f'invalid update progress sequence: {progress_summary}')
        if prov_b.commit_sha != latest_b or prov_b.requested_ref != REF:
            raise RuntimeError(f'post-update provenance is not B/main: {final}')
        if not (st_b.installed and st_b.launchable and 'Lancer' in button(card)):
            raise RuntimeError(f'post-update ProductCard/ProductState invalid: {final}')
        if not ok2 or upd2.status is not UpdateStatus.UP_TO_DATE or update_status(card) != 'Up to date':
            raise RuntimeError(f'GUI did not refresh to Up to date: {final}')

        result['stage'] = 'launch_after_update'
        card._action_button.click(); app.processEvents()
        if not process_until(lambda: bool(spawn_rec.get('pid')), 10, 50):
            raise RuntimeError('Lancer click did not spawn ZeSolver')
        launch_pid = int(spawn_rec['pid']); QThread.msleep(3000); app.processEvents()
        is_alive = alive(launch_pid); env = proc_env(launch_pid) if is_alive else {}
        launch = {**spawn_rec, 'alive_after_3s': is_alive, 'cmdline': proc_cmd(launch_pid) if is_alive else '', 'env_subset': env, 'embedded_host': env.get('ZESOLVER_EMBEDDED_HOST')}
        result['launch'] = launch; event('launch_checked', **launch)
        if not is_alive:
            raise RuntimeError(f'ZeSolver launch process not alive after 3s: {launch}')
        if env.get('ZESOLVER_EMBEDDED_HOST') != '1':
            raise RuntimeError(f'ZESOLVER_EMBEDDED_HOST missing: {env}')

        result['stage'] = 'teardown'
        termination = terminate(launch_pid); launch_pid = None
        window.close(); window.deleteLater(); app.processEvents(); QThread.msleep(200); app.processEvents()
        slash_after = sorted(str(p) for p in Path('/tmp').glob('zealfie-acq-*'))
        slash_new = sorted(set(slash_after) - set(result.get('slash_tmp_acq_before', [])))
        teardown = {'launch_stopped_or_zombie': not termination.get('running_after'), 'termination': termination, 'install_active': bool(getattr(window, 'install_active', False)), 'install_thread_none': getattr(window, '_install_thread', None) is None, 'install_worker_none': getattr(window, '_install_worker', None) is None, 'runtime_root_exists': RUNTIME_ROOT.exists(), 'work_root_size': dir_size(WORK_ROOT), 'tmp_wheelhouses_new_under_slash_tmp': slash_new, 'tmp_wheelhouses_under_slash_tmp_total': len(slash_after)}
        result['teardown'] = teardown; event('teardown', **teardown)
        if not (teardown['launch_stopped_or_zombie'] and teardown['install_thread_none'] and teardown['install_worker_none'] and not slash_new):
            raise RuntimeError(f'teardown invalid: {teardown}')

        result['status'] = 'PASS'; result['stage'] = 'pass'
        log('résumé final PASS')
        return 0
    except Exception as exc:
        result['status'] = 'FAIL'
        result['stage'] = classify(result.get('stage', 'unknown'), exc)
        result['exception'] = {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}
        log('résumé final FAIL ' + json.dumps(result['exception'], ensure_ascii=False))
        return 1
    finally:
        if launch_pid:
            try:
                terminate(launch_pid)
            except Exception:
                pass
        if window is not None:
            try:
                if not getattr(window, 'install_active', False):
                    window.close(); window.deleteLater(); app.processEvents()
            except Exception:
                pass
        result['disk_after'] = disk(RUN_ROOT)
        json_path = RUN_ROOT / 'real_gui_update_witness_result.json'
        json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        log(f'result written: {json_path}')


if __name__ == '__main__':
    raise SystemExit(main())
