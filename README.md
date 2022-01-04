TODO: Review this README and add or modify as necessary.

## ConstellixProvider provider for octoDNS

An [octoDNS](https://github.com/octodns/octodns/) provider that targets [Constellix](https://constellix.com/).

### Installation

#### Command line

```
pip install octodns_constellix
```

#### requirements.txt/setup.py

Pinning specific versions or SHAs is recommended to avoid unplanned upgrades.

##### Versions

```
# Start with the latest versions and don't just copy what's here
octodns==0.9.14
octodns_constellix==0.0.1
```

##### SHAs

```
# Start with the latest/specific versions and don't just copy what's here
-e git+https://git@github.com/octodns/octodns.git@9da19749e28f68407a1c246dfdf65663cdc1c422#egg=octodns
-e git+https://git@github.com/octodns/octodns_constellix.git@ec9661f8b335241ae4746eea467a8509205e6a30#egg=octodns_powerdns
```

### Configuration

```yaml
providers:
  constellix:
    class: octodns_constellix.ConstellixProvider
    # TODO
```

### Support Information

#### Records

TODO: All octoDNS record types are supported.

#### Dynamic

TODO: ConstellixProvider does not support dynamic records.

### Developement

See the [/script/](/script/) directory for some tools to help with the development process. They generally follow the [Script to rule them all](https://github.com/github/scripts-to-rule-them-all) pattern. Most useful is `./script/bootstrap` which will create a venv and install both the runtime and development related requirements. It will also hook up a pre-commit hook that covers most of what's run by CI.

TODO: any provider specific setup, a docker compose to run things locally etc?