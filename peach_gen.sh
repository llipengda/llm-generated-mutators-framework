#! /usr/bin/env bash
# set -euo pipefail

SLEEP_TIME=${SLEEP_TIME:-0.5}
MPE=${MPE:-3}

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <PROTO> [--udp] [--packets] [--sleep]"
    exit 1
fi

PUBLISHER=$(cat << 'EOF'
<Publisher class="TcpClient">
    <Param name="Host" value="##HOST##"/>
    <Param name="Port" value="##PORT##"/>
    <Param name="Timeout" value="200"/>
    <Param name="FaultOnConnectionFailure" value="false"/>
</Publisher>
EOF
)

if [[ " $* " == *" --udp "* ]]; then
    PUBLISHER=$(cat << 'EOF'
<Publisher class="Udp">
    <Param name="Host" value="##HOST##"/>
    <Param name="Port" value="##PORT##"/>
    <Param name="Timeout" value="200"/>
</Publisher>
EOF
)
fi

ACTION="output"
if [[ " $* " == *" --packets "* ]]; then
    ACTION="outputPackets"
fi

SLEEP=""
if [[ " $* " == *" --sleep "* ]]; then
    SLEEP="onStart=\"time.sleep(${SLEEP_TIME})\""
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')

PROTO_UPPER=$(echo "$PROTO" | tr '[:lower:]' '[:upper:]')

PWD="$(pwd)"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLM_GEN_PATH="$ROOT/llm/peach/${PROTO}"

cd "$LLM_GEN_PATH"

mcs -sdk:4 ./Mutators/*.cs \
    $(printf -- '-r:%s ' $ROOT/peach/sdk/*.dll) \
    -target:library \
    -out:./Mutators/out/${PROTO_UPPER}Mutators.dll

mcs -sdk:4 ./Fixers/*.cs \
    $(printf -- '-r:%s ' $ROOT/peach/sdk/*.dll) \
    -target:library \
    -out:./Fixers/out/${PROTO_UPPER}Fixers.dll

LLM_MUTATORS=$(grep -r "\[Mutator" ./Mutators | awk -F '"' '{print "<Mutator class=\"" $2 "\"/>"}')
MUTATOR_INCLUDE="<Mutators mode=\"include\">$LLM_MUTATORS</Mutators>"

cat << EOF > pit.llm.xml
<?xml version="1.0" encoding="utf-8"?>
<Peach xmlns="http://peachfuzzer.com/2012/Peach" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://peachfuzzer.com/2012/Peach /peach/peach.xsd">
    <Import import="time"/>
    <Include ns="dm" src="file:datamodel.xml" />
    <StateModel name="TheState" initialState="Initial">
        <State name="Initial">
            <Action type="$ACTION" $SLEEP>
                <DataModel ref="dm:${PROTO}_packet_array"/>
                <Data fileName="/seed/*.raw" />
            </Action>
        </State>
    </StateModel>
    <Test name="Default">
        <StateModel ref="TheState"/>
        $PUBLISHER
        <Strategy class="TwoPhaseRandom">
            <Param name="TwoPhaseMutation" value="True" />
            <Param name="MultipleMutationsPerElement" value="${MPE}" />
        </Strategy>
        <Logger class="File">
            <Param name="Path" value="/logs"/>
        </Logger>
    </Test>
</Peach>
EOF

cat << EOF > pit.llm.random.xml
<?xml version="1.0" encoding="utf-8"?>
<Peach xmlns="http://peachfuzzer.com/2012/Peach" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://peachfuzzer.com/2012/Peach /peach/peach.xsd">
    <Import import="time"/>
    <Include ns="dm" src="file:datamodel.xml" />
    <StateModel name="TheState" initialState="Initial">
        <State name="Initial">
            <Action type="$ACTION" $SLEEP>
                <DataModel ref="dm:${PROTO}_packet_array"/>
                <Data fileName="/seed/*.raw" />
            </Action>
        </State>
    </StateModel>
    <Test name="Default">
        <StateModel ref="TheState"/>
        $PUBLISHER
        <Strategy class="Random" />
        <Logger class="File">
            <Param name="Path" value="/logs"/>
        </Logger>
    </Test>
</Peach>
EOF

cat << EOF > pit.peach.xml
<?xml version="1.0" encoding="utf-8"?>
<Peach xmlns="http://peachfuzzer.com/2012/Peach" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://peachfuzzer.com/2012/Peach /peach/peach.xsd">
    <Import import="time"/>
    <Include ns="dm" src="file:datamodel.xml" />
    <StateModel name="TheState" initialState="Initial">
        <State name="Initial">
            <Action type="$ACTION" $SLEEP>
                <DataModel ref="dm:${PROTO}_packet_array"/>
                <Data fileName="/seed/*.raw" />
            </Action>
        </State>
    </StateModel>
    <Test name="Default">
        <StateModel ref="TheState"/>
        $PUBLISHER
        <Strategy class="Random"/>
        <Logger class="File">
            <Param name="Path" value="/logs"/>
        </Logger>
    </Test>
</Peach>
EOF

cat << EOF > pit.llm-only.xml
<?xml version="1.0" encoding="utf-8"?>
<Peach xmlns="http://peachfuzzer.com/2012/Peach" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://peachfuzzer.com/2012/Peach /peach/peach.xsd">
    <Import import="time"/>
    <Include ns="dm" src="file:datamodel.xml" />
    <StateModel name="TheState" initialState="Initial">
        <State name="Initial">
            <Action type="$ACTION" $SLEEP>
                <DataModel ref="dm:${PROTO}_packet_array"/>
                <Data fileName="/seed/*.raw" />
            </Action>
        </State>
    </StateModel>
    <Test name="Default">
        <StateModel ref="TheState"/>
        $PUBLISHER
        $MUTATOR_INCLUDE
        <Strategy class="Random" />
        <Logger class="File">
            <Param name="Path" value="/logs"/>
        </Logger>
    </Test>
</Peach>
EOF

sed "s|<DataModel name=\"${PROTO}_packet_array\">|<DataModel name=\"${PROTO}_packet_array\"><Fixup class=\"${PROTO_UPPER}Fixup\"><Param name=\"ref\" value=\"packets\"/></Fixup>|g" datamodel.xml > datamodel.fixer.xml

sed "s|file:datamodel.xml|file:datamodel.fixer.xml|g" pit.llm.xml > pit.llm.fixer.xml
sed "s|file:datamodel.xml|file:datamodel.fixer.xml|g" pit.llm-only.xml > pit.llm-only.fixer.xml

cat << EOF > Dockerfile.llm
FROM pdli/llm-peach:sdk
WORKDIR /p
ENV HOST= \\
    PORT= \\
    PEACH_ARGS=
COPY . .
RUN cp *.xml /peach/output/linux_x86_64_release/bin/ && \\
    cp Mutators/out/${PROTO_UPPER}Mutators.dll /peach/output/linux_x86_64_release/bin/Plugins/
CMD ["bash", "-c", "sed -i \"s/##HOST##/\${HOST}/g; s/##PORT##/\${PORT}/g\" /peach/output/linux_x86_64_release/bin/pit.llm.xml && \
    ls -l /peach/output/linux_x86_64_release/bin/Plugins/ && \
    cat /peach/output/linux_x86_64_release/bin/pit.llm.xml && \
    /peach/output/linux_x86_64_release/bin/peach /peach/output/linux_x86_64_release/bin/pit.llm.xml \${PEACH_ARGS}"]
EOF

cat << EOF > Dockerfile.llm.random
FROM pdli/llm-peach:sdk
WORKDIR /p
ENV HOST= \\
    PORT= \\
    PEACH_ARGS=
COPY . .
RUN cp *.xml /peach/output/linux_x86_64_release/bin/ && \\
    cp Mutators/out/${PROTO_UPPER}Mutators.dll /peach/output/linux_x86_64_release/bin/Plugins/
CMD ["bash", "-c", "sed -i \"s/##HOST##/\${HOST}/g; s/##PORT##/\${PORT}/g\" /peach/output/linux_x86_64_release/bin/pit.llm.random.xml && \
    ls -l /peach/output/linux_x86_64_release/bin/Plugins/ && \
    cat /peach/output/linux_x86_64_release/bin/pit.llm.random.xml && \
    /peach/output/linux_x86_64_release/bin/peach /peach/output/linux_x86_64_release/bin/pit.llm.random.xml \${PEACH_ARGS}"]
EOF

cat << EOF > Dockerfile.llm-only
FROM pdli/llm-peach:sdk
WORKDIR /p
ENV HOST= \\
    PORT= \\
    PEACH_ARGS=
COPY . .
RUN cp *.xml /peach/output/linux_x86_64_release/bin/ && \\
    cp Mutators/out/${PROTO_UPPER}Mutators.dll /peach/output/linux_x86_64_release/bin/Plugins/
CMD ["bash", "-c", "sed -i \"s/##HOST##/\${HOST}/g; s/##PORT##/\${PORT}/g\" /peach/output/linux_x86_64_release/bin/pit.llm-only.xml && \
    ls -l /peach/output/linux_x86_64_release/bin/Plugins/ && \
    cat /peach/output/linux_x86_64_release/bin/pit.llm-only.xml && \
    /peach/output/linux_x86_64_release/bin/peach /peach/output/linux_x86_64_release/bin/pit.llm-only.xml \${PEACH_ARGS}"]
EOF

cat << EOF > Dockerfile.llm.fixer
FROM pdli/llm-peach:sdk
WORKDIR /p
ENV HOST= \\
    PORT= \\
    PEACH_ARGS=
COPY . .
RUN cp *.xml /peach/output/linux_x86_64_release/bin/ && \\
    cp Mutators/out/${PROTO_UPPER}Mutators.dll /peach/output/linux_x86_64_release/bin/Plugins/ && \\
    cp Fixers/out/${PROTO_UPPER}Fixers.dll /peach/output/linux_x86_64_release/bin/Plugins/
CMD ["bash", "-c", "sed -i \"s/##HOST##/\${HOST}/g; s/##PORT##/\${PORT}/g\" /peach/output/linux_x86_64_release/bin/pit.llm.fixer.xml && \
    ls -l /peach/output/linux_x86_64_release/bin/Plugins/ && \
    cat /peach/output/linux_x86_64_release/bin/pit.llm.fixer.xml && \
    /peach/output/linux_x86_64_release/bin/peach /peach/output/linux_x86_64_release/bin/pit.llm.fixer.xml \${PEACH_ARGS}"]
EOF

cat << EOF > Dockerfile.llm-only.fixer
FROM pdli/llm-peach:sdk
WORKDIR /p
ENV HOST= \\
    PORT= \\
    PEACH_ARGS=
COPY . .
RUN cp *.xml /peach/output/linux_x86_64_release/bin/ && \\
    cp Mutators/out/${PROTO_UPPER}Mutators.dll /peach/output/linux_x86_64_release/bin/Plugins/ && \\
    cp Fixers/out/${PROTO_UPPER}Fixers.dll /peach/output/linux_x86_64_release/bin/Plugins/
CMD ["bash", "-c", "sed -i \"s/##HOST##/\${HOST}/g; s/##PORT##/\${PORT}/g\" /peach/output/linux_x86_64_release/bin/pit.llm-only.fixer.xml && \
    ls -l /peach/output/linux_x86_64_release/bin/Plugins/ && \
    cat /peach/output/linux_x86_64_release/bin/pit.llm-only.fixer.xml && \
    /peach/output/linux_x86_64_release/bin/peach /peach/output/linux_x86_64_release/bin/pit.llm-only.fixer.xml \${PEACH_ARGS}"]
EOF

cat << EOF > Dockerfile.peach
FROM pdli/llm-peach:sdk
WORKDIR /p
ENV HOST= \\
    PORT= \\
    PEACH_ARGS=
COPY . .
RUN cp *.xml /peach/output/linux_x86_64_release/bin/ 
CMD ["bash", "-c", "sed -i \"s/##HOST##/\${HOST}/g; s/##PORT##/\${PORT}/g\" /peach/output/linux_x86_64_release/bin/pit.peach.xml && \
    ls -l /peach/output/linux_x86_64_release/bin/Plugins/ && \
    cat /peach/output/linux_x86_64_release/bin/pit.peach.xml && \
    /peach/output/linux_x86_64_release/bin/peach /peach/output/linux_x86_64_release/bin/pit.peach.xml \${PEACH_ARGS}"]
EOF

docker build -t "pdli/llm-peach:${PROTO}-llm" -f Dockerfile.llm . --platform=linux/amd64
docker build -t "pdli/llm-peach:${PROTO}-llm-fixer" -f Dockerfile.llm.fixer . --platform=linux/amd64
docker build -t "pdli/llm-peach:${PROTO}-peach" -f Dockerfile.peach . --platform=linux/amd64
docker build -t "pdli/llm-peach:${PROTO}-llm-random" -f Dockerfile.llm.random . --platform=linux/amd64
docker build -t "pdli/llm-peach:${PROTO}-llm-only" -f Dockerfile.llm-only . --platform=linux/amd64
docker build -t "pdli/llm-peach:${PROTO}-llm-only-fixer" -f Dockerfile.llm-only.fixer . --platform=linux/amd64

cd "$PWD"