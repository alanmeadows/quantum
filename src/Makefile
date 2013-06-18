CC ?= gcc
CFLAGS ?= -O2 -g -Werror -Wall

all: dhcp_agent_dnsmasq_lease_update

dhcp_agent_dnsmasq_lease_update: dhcp_agent_dnsmasq_lease_update.o
	$(CC) $(CFLAGS) -o $@ $^

clean:
	rm -f dhcp_agent_dnsmasq_lease_update.o dhcp_agent_dnsmasq_lease_update
