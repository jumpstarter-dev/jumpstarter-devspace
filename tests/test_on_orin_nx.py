import logging
import sys
import time

import opendal
import pexpect
import pytest
from jumpstarter_imagehash import ImageHash

from jumpstarter.client.adapters import PexpectAdapter
from jumpstarter.testing.pytest import JumpstarterTest

log = logging.getLogger(__file__)

PROMPT = "root@localhost ~]#"
_booted_and_logged = False


class TestOrinNx(JumpstarterTest):
    filter_labels = {"board": "orin-nx"}

    @pytest.fixture()
    def console(self, client):
        with PexpectAdapter(client=client.interface.console) as console:
            if True:
                console.logfile_read = sys.stdout.buffer
            yield console

    @pytest.fixture()
    def video(self, client):
        return ImageHash(client.video)

    @pytest.fixture()
    def shell(self, client, console):
        client.interface.power.off()
        time.sleep(1)
        client.interface.storage.dut()
        client.interface.power.on()
        yield _wait_and_login(console, "root", "redhat")
        self._power_off(client, console)

    @pytest.fixture()
    def booted_shell(self, client, console):
        global _booted_and_logged
        if _booted_and_logged:
            return console
        log.info("No booted console, booting")
        client.interface.power.off()
        time.sleep(1)
        client.interface.storage.dut()
        client.interface.power.on()
        c = _wait_and_login(console, "root", "redhat")
        _booted_and_logged = True
        log.info("A booted shell is ready")
        return c

    def test_setup_device(self, client, console):
        client.interface.power.off()
        log.info("Setting up device")
        try:
            client.interface.storage.write_local_file("./output/image/disk.raw")
        except opendal.exceptions.NotFound:
            pytest.exit("No image file found")
            return
        client.interface.storage.dut()
        client.interface.power.on()
        console.logfile_read = sys.stdout.buffer
        # first boot on raspbian will take some time, we wait for the login
        shell = _wait_and_login(console, "root", "redhat")
        shell.sendline("")
        # then power off the device
        self._power_off(client, console)


    def test_power_on_hdmi(self, client, video, console):
        client.interface.storage.dut()
        # check all the image snapshots through the rpi4 boot process
        client.interface.power.on()
        for i in range(300):
            time.sleep(0.1)
            sn = video.snapshot()
            sn.save("video.jpeg")
            #sn.save(f"image_{i}.jpeg")
            try:
                video.assert_snapshot("tests/test_booted_ok.jpeg")
                break # once we see this, exit the loop
            except AssertionError:
                continue

        sn = video.snapshot()
        sn.save("video.jpeg")
        video.assert_snapshot("tests/test_booted_ok.jpeg")
        client.interface.power.off()

    def test_devices_nvidia(self, booted_shell):
        res, out = _cmd(booted_shell, 'find /dev -name "*nv*"')
        out = out.replace(b"\r", b"").replace(b"\n", b" ")
        log.info("nv devices found: %s", out)
        assert b"nvidia0" in out, "Devices should contain the nvidia devices"
        assert b"nvgpu" in out, "Devices should contain the nvgpu devices"
        assert res == 0

    def test_devices_devices(self, booted_shell):
        res, out = _cmd(booted_shell, 'find /dev -name "*tegra*"')
        log.info("tegra devices found: %s", out)
        assert b"tegra" in out, "Devices should contain the tegra devices"
        assert res == 0

    def test_devices_video(self, booted_shell):
        res, out = _cmd(booted_shell, 'find /dev/dri -name "card*" ')
        log.info("video devices found: %s", out)
        assert b"card0" in out, "Devices should contain the video devices"
        assert res == 0

    def test_pull_cuda_samples(self, booted_shell):
        res, out = _cmd(booted_shell, "podman pull quay.io/sroyer/jetpack-6-cuda-12.2-samples:latest")
        assert b"Writing manifest to image destination" in out
        assert res == 0


    def test_login_console_hdmi(self, shell, video):
        video.assert_snapshot("tests/test_booted_ok.jpeg" ,1)


    def _power_off(self, client, console):
        global _booted_and_logged
        log.info("Attempting a soft power off")
        try:
            console.sendline("poweroff")
            console.expect("System Power Off", timeout=90)
        except pexpect.TIMEOUT:
            log.error("Timeout waiting for power down, continuing with hard power off")
        finally:
            _booted_and_logged = False
            log.info("No booted shell")
            client.interface.power.off()
            time.sleep(2)


def _wait_and_login(c, username, password, timeout=120):
    log.info("Waiting for login prompt")
    try:
        c.expect("login:", timeout=timeout)
    except pexpect.exceptions.TIMEOUT:
        c.sendline("") # sometimes we could have had noisy kernel messages on the console
        c.expect("login:", timeout=5)
    c.sendline(username)
    c.expect("Password:", timeout=120)
    c.sendline(password)
    print("") # so the log does not overlap on top of the "Password:"
    log.info("Logged in")
    # 2 is critical (current, default, minimum, boot-time-default)
    _cmd(c, 'sysctl -w kernel.printk="2 4 1 7"')
    _cmd(c, "stty rows 100 cols 200")
    return c

def _cmd(c, cmd, timeout=240):
    # wait for the prompt and send a command
    try:
        c.sendline("")
        c.expect(PROMPT, timeout=10)
        try:
            c.expect(PROMPT, timeout=1) # if we really had a waiting prompt, our sendline generated another
        except pexpect.exceptions.TIMEOUT:
            pass
    except pexpect.exceptions.TIMEOUT:
        log.warning("We timed out waiting for prompt %s", PROMPT)
        pass

    c.sendline(cmd)

    # wait for the prompt and try get the result
    c.expect(PROMPT, timeout=timeout)
    # save the console output
    output = c.before

    save = c.logfile_read
    try:
        # hide the result capture
        c.logfile_read = None
        c.sendline("echo __CMDRESULT__: $?")
        c.expect(r"__CMDRESULT__: \d.", timeout=10)
    finally:
        c.logfile_read = save

    print("")
    res = c.after.decode().strip()
    parts = res.split(" ")
    assert parts[0] == "__CMDRESULT__:"

    # process the command output and remove any trailing data, remove right until after the command
    cmd = bytearray(cmd, "utf-8")
    try:
        output = output[output.index(cmd) + len(cmd):]
        output = output[output.index(b"\n") + 1:]
    except ValueError:
        output = b""

    # look for the return carriage / new line and remove until then
    try:
        output = output[output.index(b"\r") + 1:]
    except ValueError:
        output = b""

    # at this point output holds exactly the command output
    # parts[1] contains the exit value of the called shell command
    return int(parts[1]), output
