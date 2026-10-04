"""Store calls from several threads at once - what run-all does with its web,
supervisor and agent-daemon threads in one process."""

import threading

import kuska as ac


def test_threads_each_keep_their_own_database(tmp_path):
    """Model binding is per thread: one thread finishing a call must not unbind
    the models under another (peewee.ImproperlyConfigured mid-init_db), nor
    send another thread's queries to its own database."""
    paths = [tmp_path / "a.db", tmp_path / "b.db"]
    for path in paths:
        db = ac.connect(path)
        ac.init_db(db)
        db.close()

    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def worker(n: int) -> None:
        db = ac.connect(paths[n % 2])
        try:
            start.wait()
            for i in range(50):
                ac.init_db(db)
                ac.register_agent(db, f"t{n}-{i}", "claude")
                ac.list_tasks(db)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    for k, path in enumerate(paths):
        db = ac.connect(path)
        names = {a["name"] for a in ac.list_agents(db)}
        db.close()
        assert names == {f"t{n}-{i}" for n in range(k, 8, 2) for i in range(50)}
