def _get_deployed_wallet(output):
    for line in output.splitlines():
        if line.startswith("CaravanProxy deployed: "):
            return line.rsplit(" ", 1)[1]

    raise AssertionError(output)


def test_deploy_new_wallet(create_wallet):
    result = create_wallet()
    assert "CaravanProxy deployed" in result.output, result.output


def test_deployed_wallet_in_list(run_cmd, create_wallet):
    result = create_wallet()
    wallet = _get_deployed_wallet(result.output)

    result = run_cmd("track", wallet, "31337")
    assert result.output == "", result.output

    result = run_cmd("list")
    assert wallet in result.output, result.output

    result = run_cmd("unlink", wallet, prompt_args="\n".join(["y"]))
    assert wallet in result.output, result.output

    result = run_cmd("list")
    assert result.output == "No wallets being tracked!\n", result.output

    result = run_cmd("track", wallet, "31337")
    assert result.output == "", result.output

    result = run_cmd("list")
    assert wallet in result.output, result.output
