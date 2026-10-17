# The environment's cells (comment with a { brace)
cells = {
  dev-int-stateless-az1 = { address_space = "172.22.0.0/20", enabled = false }
  dev-int-stateless-az2 = { address_space = "172.22.16.0/20" }
  dev-int-stateless-az3 = { address_space = "100.64.0.0/16", pod_network = "subnet" }
}
