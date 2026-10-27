function svc
    switch $argv[1]
        case start stop
            echo $argv[1]
        case status
            echo ok
        case -h --help
            echo help
        case '--log=*'
            echo log
    end
end
