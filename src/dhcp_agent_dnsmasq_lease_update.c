#define _GNU_SOURCE

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

static int
parse_long(long *out, const char *str, int base)
{
    char *end;
    errno = 0;
    *out = strtol(str, &end, base);
    if (errno)
        return -errno;
    if (*end != '\0')
        return -EINVAL;
    return 0;
}

static int
file_exists(const char *path)
{
    struct stat buf;
    return stat(path, &buf) == 0;
}

static char*
json_encoded(const char *str)
{
    size_t len;
    size_t i;
    char *r;
    char *c;

    len = 2;
    for (i = 0; i < strlen(str); i++)
    {
        switch (str[i])
        {
        case '"':
        case '\\':
            len += 2; /* \" or \\ */
            break;
        case 0 ... 0x1f:
            len += 6; /* \uXXXX */
            break;
        default:
            len += 1;
        }
    }

    r = malloc(len + 1);
    if (r == NULL)
        return r;

    c = r;
    *c++ = '"';
    for (i = 0; i < strlen(str); i++)
    {
        switch (str[i])
        {
        case '"':
        case '\\':
            *c++ = '\\';
            *c++ = str[i];
            break;
        case 0 ... 0x1f:
            c += sprintf(c, "\\u00%02x", str[i]);
            break;
        default:
            *c++ = str[i];
        }
    }
    *c++ = '"';
    *c = '\0';

    return r;
}

int main(int argc, char *argv[])
{
    int r;
    long lease_remaining;
    char *network_id;
    char *dhcp_relay_socket;
    char *mac_address;
    char *ip_address;

    if (argc < 2)
    {
        fprintf(stderr, "%s: need action\n", argv[0]);
        return 1;
    }

    if (strcmp(argv[1], "add") &&
        strcmp(argv[1], "del") &&
        strcmp(argv[1], "old"))
    {
        return 0;
    }

    if (argc < 4)
    {
        fprintf(stderr, "%s: need mac_address, ip_address\n", argv[0]);
        return 1;
    }

    network_id = getenv("QUANTUM_NETWORK_ID");
    if (!network_id)
        network_id = strdup("null");
    else
        network_id = json_encoded(network_id);

    dhcp_relay_socket = getenv("QUANTUM_RELAY_SOCKET_PATH");
    if (!dhcp_relay_socket)
    {
        fprintf(stderr, "%s: QUANTUM_RELAY_SOCKET_PATH missing from env\n",
                argv[0]);
        return 1;
    }

    mac_address = json_encoded(argv[2]);
    ip_address = json_encoded(argv[3]);

    if (strcmp(argv[1], "del") && getenv("DNSMASQ_TIME_REMAINING"))
    {
        r = parse_long(&lease_remaining, getenv("DNSMASQ_TIME_REMAINING"), 0);
        if (r)
        {
            fprintf(stderr, "%s: could not parse DNSMASQ_TIME_REMAINING (%s)\n",
                    argv[0], getenv("DNSMASQ_TIME_REMAINING"));
            return 1;
        }
    }
    else
    {
        lease_remaining = 0;
    }

    if (file_exists(dhcp_relay_socket))
    {
        int fd;
        int datalen;
        struct sockaddr_un address;
        char *data;

        fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0)
        {
            fprintf(stderr, "%s: socket() failed\n", argv[0]);
            return 1;
        }

        memset(&address, 0, sizeof(address));
        address.sun_family = AF_UNIX;
        if (strlen(dhcp_relay_socket) >= sizeof(address.sun_path))
        {
            fprintf(stderr, "%s: socket path (%s) too long to fit in "
                            "address struct\n",
                    argv[0], dhcp_relay_socket);
            return 1;
        }
        strcpy(address.sun_path, dhcp_relay_socket);

        r = connect(fd, (struct sockaddr*) &address, sizeof(address));
        if (r)
        {
            fprintf(stderr, "%s: connect() failed\n", argv[0]);
            return 1;
        }

        r = asprintf(&data, "{\"network_id\":%s,"
                            " \"mac_address\":%s,"
                            " \"ip_address\":%s,"
                            " \"lease_remaining\":%ld}",
                     network_id, mac_address, ip_address, lease_remaining);
        if (r < 0)
        {
            fprintf(stderr, "%s: asprintf failed\n", argv[0]);
            return 1;
        }
        datalen = r;

        r = write(fd, data, datalen);
        if (r != datalen)
        {
            fprintf(stderr, "%s: write failed\n", argv[0]);
            return 1;
        }
    }
    return 0;
}
