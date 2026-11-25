/*
 * OpenSpeaksy.app's executable: starts the Python app as its child and
 * waits for it.
 *
 * macOS files every permission (Accessibility, Input Monitoring, Microphone)
 * under the process responsible for the one asking, and a spawned child stays
 * the responsibility of its parent. So with this in between, System Settings
 * lists "OpenSpeaksy" rather than Homebrew's "Python", and a grant survives a
 * Python upgrade. Exec'ing Python instead would hand the identity over to it.
 *
 * Usage: OpenSpeaksy program [args...]  (the LaunchAgent passes the venv's
 * python and main.py). Exits with the child's status, so launchd's KeepAlive
 * sees the app's own exit code.
 */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s program [args...]\n", argv[0]);
        return 64;
    }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);

    pid_t pid;
    int err = posix_spawn(&pid, argv[1], NULL, NULL, argv + 1, environ);
    if (err) {
        fprintf(stderr, "OpenSpeaksy: cannot start %s: %s\n", argv[1], strerror(err));
        return 1;
    }
    child = pid;

    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return 128 + WTERMSIG(status);
}
