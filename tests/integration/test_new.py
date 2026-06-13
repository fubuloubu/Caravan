def test_deploy_new_wallet(create_wallet):
    result = create_wallet()
    assert "CaravanProxy deployed" in result.output, result.output


def test_deployed_wallet_in_list(run_cmd, create_wallet):
    create_wallet()
    result = run_cmd("list")
    assert "0x..." in result.output, result.output

    result = run_cmd("unlink", "0x...", prompt_args="\n".join(["y"]))
    assert "0x..." in result.output, result.output

    result = run_cmd("list")
    assert result.output == "", result.output

    result = run_cmd("track", "0x...")
    assert "tracked" in result.output, result.output

    result = run_cmd("list")
    assert "0x..." in result.output, result.output
