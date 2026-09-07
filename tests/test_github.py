from types import SimpleNamespace
import unittest

from pg_hub.github import GitHubClient


class GitHubClientTests(unittest.TestCase):
    def client(self) -> GitHubClient:
        config = SimpleNamespace(
            github_repository="example/pg_hub",
            github_api_url="https://api.github.test",
        )
        return GitHubClient(config, SimpleNamespace())  # type: ignore[arg-type]

    def test_comment_temporarily_unlocks_and_relocks_issue(self) -> None:
        client = self.client()
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, payload=None):
            calls.append((method, path, payload))
            if path.endswith("/comments?per_page=100"):
                return []
            if method == "GET" and path.endswith("/issues/12"):
                return {"locked": True}
            return None

        client.request = request  # type: ignore[method-assign]
        client.add_comment(12, "reply", "marker")

        self.assertEqual(
            calls[2:],
            [
                ("DELETE", "repos/example/pg_hub/issues/12/lock", None),
                ("POST", "repos/example/pg_hub/issues/12/comments", {"body": "reply"}),
                ("PUT", "repos/example/pg_hub/issues/12/lock", {}),
            ],
        )

    def test_comment_temporarily_unlocks_and_relocks_discussion(self) -> None:
        client = self.client()
        client._discussion = lambda number, include_comments=False: {  # type: ignore[method-assign]
            "id": "discussion-id",
            "number": number,
            "locked": True,
            "comments": {"nodes": []},
        }
        operations: list[str] = []

        def graphql(query: str, variables: dict[str, str]):
            del variables
            operations.append(query)
            return {}

        client.graphql = graphql  # type: ignore[method-assign]
        client.add_discussion_comment(3, "reply", "marker")

        self.assertIn("unlockLockable", operations[0])
        self.assertIn("addDiscussionComment", operations[1])
        self.assertIn("lockLockable", operations[2])


if __name__ == "__main__":
    unittest.main()
